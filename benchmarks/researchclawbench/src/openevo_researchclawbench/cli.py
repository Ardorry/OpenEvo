"""No-surprise CLI for native-path audits and gated canary submission."""

from __future__ import annotations

import argparse
import json
import statistics
from datetime import UTC, datetime
from pathlib import Path

from .candidate_reconciliation import (
    CandidateReconciliationSpec,
    ValidatorTerminalReconciliationSpec,
    reconcile_candidate_sealed_authority,
    reconcile_invalid_validator_terminal,
)
from .candidate_runner import candidate_source_audit
from .community_evaluator import (
    run_judge_environment_diagnostic,
    run_judge_probe_subprocess,
)
from .config import (
    CANARY_TASK,
    FROZEN_TASKS,
    MANAGED_CODEX_MODEL,
    ExperimentConfig,
    ProtocolError,
)
from .contamination_audit import run_offline_contamination_audit
from .deepseek_codex_engineering_port import (
    DeepSeekCodexEngineeringPort,
)
from .delivery_canary_v3 import (
    DeliveryCanaryConfig,
    DeliveryCanaryDryRunCandidate,
    DeliveryCanaryV3Runner,
)
from .durable_evaluator_operation import OPENROUTER_API_BASE
from .evolution_loop import FrozenTrainingSchedule
from .formal_v11 import (
    FormalV11RuntimeAssetOverrides,
    bind_formal_core_identity,
    prepare_formal_v11_protocol,
    validate_formal_v11_identity,
)
from .hard_gt_teacher import native_teacher_attachment_capability
from .managed_core_control import (
    ManagedCoreControlUnavailable,
    acquire_managed_core_control,
    managed_core_host_profile_readiness,
    managed_core_profile_readiness,
)
from .minimal_per_item_runner import (
    DryRunCandidatePort,
    DryRunEvolutionPort,
    DryRunJudgePort,
    ExistingJudgeAdapter,
    MinimalPerItemConfig,
    MinimalPerItemRunner,
)
from .native_evolution_recovery import (
    NativeEvolutionRecoveryAdapter,
    native_evolution_recovery_dry_run,
)
from .official_training_control import (
    OfficialTrainingControl,
    OfficialTrainingOperationsUnavailable,
    build_production_official_operations,
    validate_official_control,
)
from .official_training_supervisor import OfficialTrainingStage
from .per_item_pilot_v2 import (
    PilotV2Config,
    PilotV2DryRunCandidate,
    PilotV2DryRunEvolution,
    PilotV2DryRunJudge,
    PilotV2Error,
    PilotV2JudgePort,
    PilotV2Runner,
)
from .production_operation_ports import build_production_ports
from .production_training_operations import (
    JudgeCredentialsRequired,
    judge_credential_readiness,
    judge_identity_preflight,
)
from .reflector_runner import reflector_runtime_audit
from .rejudge_sealed import RejudgeSealedError, run_rejudge_sealed
from .run_manifest import atomic_write_json
from .training_control import (
    DurableTrainingControl,
    TrainingOperationsUnavailable,
    print_closed_json,
)
from .training_supervisor import supervisor_capability_audit
from .transition_engine import TrainingStage
from .validator_failure_learning import (
    ValidatorFailureLearningSpec,
    ValidatorLearningPreEffectReplacementSpec,
    initialize_validator_failure_learning,
    replace_validator_learning_post_feedback_pre_evolution_namespace,
    replace_validator_learning_pre_effect_namespace,
)

_FORMAL_TERMINAL_STAGES = {
    TrainingStage.FINAL_FROZEN.value,
    TrainingStage.BUDGET_EXHAUSTED.value,
    TrainingStage.BLOCKED.value,
    TrainingStage.FAILED.value,
    TrainingStage.TASK_NO_VALID_ATTEMPT.value,
    TrainingStage.CANDIDATE_SETUP_BLOCKED.value,
    TrainingStage.ITEM_RESET.value,
}

_OFFICIAL_TERMINAL_STAGES = {
    OfficialTrainingStage.COMPLETE.value,
    OfficialTrainingStage.BLOCKED.value,
    OfficialTrainingStage.FAILED.value,
}


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
    reflector = reflector_runtime_audit(config.openevo_root)
    teacher = native_teacher_attachment_capability(config.openevo_root)
    supervisor = supervisor_capability_audit(_package_root())
    receipt = {
        "schema_version": "1.0.0",
        "checked_at": datetime.now(UTC).isoformat(),
        "candidate": candidate,
        "reflector": reflector,
        "teacher": teacher,
        "formal_training_orchestrator": supervisor,
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
    if receipt["passed"]:
        receipt["status"] = "STATIC_AUDIT_PASS_RUNTIME_PROBES_REQUIRED"
    elif (
        teacher["production_evolution_http_transport"]
        and teacher["production_science_successor_hook"]
        and supervisor.get("durable_state_machine_ready")
        and not supervisor.get("production_operations_bound")
    ):
        receipt["status"] = "BLOCKED_UNRESOLVED_DURABILITY_GAP"
    else:
        receipt["status"] = "BLOCKED_BY_OPENEVO_NATIVE_CAPABILITY_GAP"
    _write(config.experiment_root / "manifests/native_path_static_audit.json", receipt)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt["passed"] else 5


def command_readiness(config: ExperimentConfig) -> int:
    receipt = config.environment_readiness()
    authority = None
    try:
        authority = acquire_managed_core_control(config)
        core_control = authority.public_readiness()
        host_profile = core_control.get("managed_host_profile")
        if not isinstance(host_profile, dict):
            host_profile = managed_core_profile_readiness(config)
            core_control["managed_host_profile"] = host_profile
        if not host_profile["ready"]:
            receipt["ready"] = False
            if "MANAGED_CORE_HOST_PROFILE_UNAVAILABLE" not in receipt["blockers"]:
                receipt["blockers"].append("MANAGED_CORE_HOST_PROFILE_UNAVAILABLE")
    except ManagedCoreControlUnavailable:
        core_control = {
            "service_reachable": False,
            "bearer_present": False,
            "bearer_valid": False,
            "generation_matches": False,
            "candidate_port_authorized": False,
            "secret_recorded": False,
            "environment_fallback_used": False,
        }
        receipt["ready"] = False
        if "CORE_CONTROL_AUTHORITY_UNAVAILABLE" not in receipt["blockers"]:
            receipt["blockers"].append("CORE_CONTROL_AUTHORITY_UNAVAILABLE")
    finally:
        if authority is not None:
            authority.close()
    receipt.update(
        {
            "checked_at": datetime.now(UTC).isoformat(),
            "core_control": core_control,
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


def command_formal_community_validate(config: ExperimentConfig, run_id: str) -> dict[str, object]:
    identity = validate_formal_v11_identity(config)
    formal = config.formal_runs_v11
    if formal is None or formal["community"]["run_id"] != run_id:
        raise ProtocolError("formal Community run ID differs from the v11 protocol")
    state_root = config.experiment_root / "supervisor" / run_id
    namespace_available = not state_root.exists()
    readiness = config.environment_readiness()
    blockers = list(readiness["blockers"])
    if not namespace_available:
        blockers.append("FORMAL_COMMUNITY_NAMESPACE_ALREADY_EXISTS")
    return {
        "status": "PASS" if not blockers else "BLOCKED",
        "mode": "fresh_community17_validate_only",
        "run_id": run_id,
        "scope": "community-17",
        "namespace_available": namespace_available,
        "protocol_sha256": identity["protocol_sha256"],
        "formal_identity_sha256": identity["content_sha256"],
        "community_tasks": 17,
        "candidate_runs": 51,
        "reflector_cycles": 34,
        "artifact_evolution_jobs": 102,
        "legacy_authority_import_allowed": False,
        "ready": not blockers,
        "blockers": blockers,
        "model_started": False,
        "judge_started": False,
        "secret_recorded": False,
    }


def command_formal_community_live_preflight(
    config: ExperimentConfig,
    run_id: str,
) -> dict[str, object]:
    """Run live, no-model production probes before namespace initialization."""

    static = command_formal_community_validate(config, run_id)
    if not static["ready"]:
        return static
    authority = acquire_managed_core_control(config)
    try:
        run_root = config.experiment_root / "formal_preflight" / run_id
        ports = build_production_ports(
            config,
            run_root,
            core_authority=authority,
        )
        candidate = ports.candidate.preflight(
            {
                "task_id": FROZEN_TASKS[0],
                "core_control_generation": authority.generation,
                "core_control_release_identity": authority.release_identity,
            }
        )
        reflector = ports.evolution.preflight({})
        judge = judge_credential_readiness(config)
        judge_identity = judge_identity_preflight(
            config,
            receipt_path=run_root / "evaluator_private" / "judge-preflight.json",
        )
        readiness = authority.public_readiness()
        native_feedback = native_teacher_attachment_capability(config.openevo_root)
        required_core = (
            "service_reachable",
            "bearer_present",
            "bearer_valid",
            "generation_matches",
            "candidate_port_authorized",
        )
        candidate_isolation = candidate.get("managed_candidate_subscription_isolation")
        candidate_isolation_ready = isinstance(candidate_isolation, dict) and all(
            candidate_isolation.get(field) is expected
            for field, expected in {
                "authority_issued": True,
                "mount_adopted": True,
                "codex_cli_auth_visible": True,
                "host_source_hidden": True,
                "tool_sandbox_credential_hidden": True,
                "tool_environment_clean": True,
                "parent_process_secret_unreadable": True,
                "generation_matches": True,
                "release_identity_matches": True,
                "image_digest_matches": True,
                "cli_version_matches": True,
                "cleanup_verified": True,
                "model_started": False,
            }.items()
        )
        capability_receipt = {
            "candidate_core_capabilities": candidate.get("project_configuration_valid") is True,
            "candidate_generation_matches": candidate.get("generation") == authority.generation,
            "candidate_release_identity_matches": candidate.get("release_identity")
            == authority.release_identity,
            "managed_candidate_credential_adoption": all(
                candidate.get(field) is expected
                for field, expected in {
                    "bearer_present": True,
                    "bearer_valid": True,
                    "candidate_port_authorized": True,
                    "secret_recorded": False,
                    "environment_fallback_used": False,
                    "managed_candidate_runtime_ready": True,
                    "managed_candidate_credential_mount_adopted": True,
                    "managed_candidate_model_started": False,
                }.items()
            )
            and candidate.get("managed_candidate_codex_cli_version") == "0.144.1",
            "managed_candidate_subscription_isolation": (candidate_isolation_ready),
            "managed_reflector_credential_adoption": reflector.get("ready") is True
            and reflector.get("identity_matches") is True,
            "judge_identity_ready": judge["ready"] is True
            and judge_identity.get("api_key_present") is True
            and judge_identity.get("api_base") == OPENROUTER_API_BASE
            and judge_identity.get("model") == config.require("judge.model")
            and judge_identity.get("provider") == config.require("judge.provider")
            and judge_identity.get("judge_request_started") is False
            and judge_identity.get("model_started") is False
            and judge_identity.get("secret_recorded") is False,
            "durable_attachment_capability": native_feedback.get(
                "production_evolution_http_transport"
            )
            is True,
            "successor_capability": native_feedback.get("production_science_successor_hook")
            is True,
            "triple_registry_capability": candidate.get("project_configuration_valid") is True,
            "final_freeze_capability": hasattr(ports.freeze, "execute")
            and hasattr(ports.freeze, "recover"),
        }
        blockers = [
            key.upper() + "_NOT_READY"
            for key, value in capability_receipt.items()
            if value is not True
        ]
        if any(readiness.get(field) is not True for field in required_core):
            blockers.append("FORMAL_CORE_CONTROL_NOT_READY")
        if readiness.get("secret_recorded") is not False:
            blockers.append("FORMAL_CORE_SECRET_BOUNDARY_NOT_READY")
        if not blockers:
            bind_formal_core_identity(
                config=config,
                run_id=run_id,
                authority_readiness=readiness,
            )
        return {
            **static,
            "status": "PASS" if not blockers else "BLOCKED",
            "ready": not blockers,
            "blockers": blockers,
            "live_no_model_preflight": capability_receipt,
            "core_generation": authority.generation,
            "release_identity": authority.release_identity,
            "model_started": False,
            "judge_started": False,
            "candidate_intent_persisted": False,
            "evolution_intent_persisted": False,
            "secret_recorded": False,
        }
    finally:
        authority.close()


def _run_formal_community_transition(
    *,
    config: ExperimentConfig,
    run_id: str,
    operation: str,
) -> dict[str, object]:
    """Execute one transition with an operation-scoped managed attachment."""

    authority = acquire_managed_core_control(config)
    try:
        if not authority.managed_host_profile_ready:
            raise ManagedCoreControlUnavailable(
                "formal training requires docker_user_container_v1 host profile"
            )
        bind_formal_core_identity(
            config=config,
            run_id=run_id,
            authority_readiness=authority.public_readiness(),
        )
        control = DurableTrainingControl(
            config=config,
            run_id=run_id,
            require_existing=True,
            task_ids=FROZEN_TASKS,
            production=True,
            core_control_authority=authority,
        )
        method = getattr(control, operation)
        return method()
    finally:
        authority.close()


def _run_formal_official_transition(
    *,
    config: ExperimentConfig,
    community_run_id: str,
    official_run_id: str,
    operation: str,
) -> dict[str, object]:
    """Execute one official transition with a fresh managed attachment."""

    authority = acquire_managed_core_control(config)
    try:
        if not authority.managed_host_profile_ready:
            raise ManagedCoreControlUnavailable(
                "formal official run requires docker_user_container_v1 host profile"
            )
        bind_formal_core_identity(
            config=config,
            run_id=official_run_id,
            authority_readiness=authority.public_readiness(),
        )
        operations = build_production_official_operations(
            config=config,
            community_run_id=community_run_id,
            official_run_id=official_run_id,
            core_authority=authority,
        )
        control = OfficialTrainingControl(
            config=config,
            community_run_id=community_run_id,
            official_run_id=official_run_id,
            operations=operations,
            production=True,
            require_existing=True,
        )
        method = getattr(control, operation)
        return method()
    finally:
        authority.close()


def command_minimal_per_item(args: argparse.Namespace) -> int:
    """Run the minimal single-task per-item canary (dry-run or live)."""

    config = MinimalPerItemConfig.load(args.protocol)
    config.output_root.mkdir(parents=True, exist_ok=True)
    dry_run = bool(args.dry_run or args.no_model_calls)
    if dry_run:
        candidate_port = DryRunCandidatePort(model=config.candidate_model)
        evolution_port = DryRunEvolutionPort()
        judge_port = DryRunJudgePort(model=config.judge_model)
    else:
        candidate_port = DeepSeekCodexEngineeringPort(
            model=config.candidate_model,
            timeout_seconds=int(config.require("candidate.timeout_seconds")),
            sandbox=str(config.require("candidate.sandbox")),
        )
        evolution_port = DeepSeekCodexEngineeringPort(
            model=config.candidate_model,
            timeout_seconds=int(config.require("evolution.timeout_seconds"))
            if "timeout_seconds" in config.raw.get("evolution", {})
            else 1800,
        )
        judge_port = ExistingJudgeAdapter(
            config,
            evaluator_private_root=config.output_root / "evaluator_private" / args.run_id,
            timeout_seconds=int(config.require("judge.timeout_seconds")),
        )
    state_root = config.output_root / "supervisor" / args.run_id
    fresh = not (state_root / "training-supervisor.sqlite3").is_file()
    runner = MinimalPerItemRunner(
        config=config,
        state_root=state_root,
        run_id=args.run_id,
        candidate_port=candidate_port,
        evolution_port=evolution_port,
        judge_port=judge_port,
    )
    if fresh:
        runner.initialize()
    state = runner.run_until_item_closed()
    print_closed_json(state)
    return 0 if state.get("stage") == "COMPLETE" else 5


def _print_per_item_runner_summary(
    config: ExperimentConfig,
    *,
    task_id: str,
    run_id: str,
    dry_run: bool,
    baseline_only: bool = False,
) -> None:
    """Print the non-secret meeting summary before any mutable operation."""

    output_root = config.experiment_root / "supervisor" / run_id
    evolution_lines = (
        ("  not executed in next-task reset validation",)
        if baseline_only
        else (
            "  backend: OpenEvo native successor/reflector",
            f"  model: {MANAGED_CODEX_MODEL}",
            "  artifacts:",
            "    - memory",
            "    - skill",
            "    - agent-system",
            "  supervision: sanitized evaluator feedback + baseline evidence capsule",
            "  raw GT: excluded from reflector",
            "  Judge reasoning: excluded",
        )
    )
    judge_lines = (
        ("  not executed in next-task reset validation",)
        if baseline_only
        else (
            "  backend: OpenRouter",
            "  model: openai/gpt-5.1",
            "  provider: Azure",
        )
    )
    pass_lines = (
        ("  validate and seal baseline", "  stop")
        if baseline_only
        else (
            "  judge baseline",
            "  project candidate-specific retention feedback",
            "  build baseline evidence capsule",
            "  admit candidate-specific retention feedback",
            "  evolve once with candidate-specific retention feedback",
            "  verify task-specific native artifacts",
            "  evolved",
            "  judge evolved",
            "  paired result",
            "  reset",
        )
    )
    print(
        "\n".join(
            (
                "ResearchClawBench Per-Item Runner",
                "=================================",
                "",
                f"Protocol: {config.require('protocol_name')}",
                f"Task: {task_id}",
                f"Run ID: {run_id}",
                "Dataset: community",
                "",
                "Candidate:",
                "  backend: OpenEvo Core / CodexHarness",
                f"  model: {MANAGED_CODEX_MODEL}",
                "  runtime: managed",
                "",
                "Evolution:",
                *evolution_lines,
                "",
                "Judge:",
                *judge_lines,
                "",
                "Passes:",
                "  baseline",
                *pass_lines,
                "",
                "Isolation:",
                "  fresh Codex per pass",
                "  fresh state per task",
                "  GT hidden from Candidate",
                "  raw GT and Judge reasoning excluded from Evolution",
                "  sanitized evaluation feedback plus baseline evidence capsule visible only to Reflector",
                "  cross-task artifacts excluded",
                "",
                "Output:",
                f"  {output_root}",
                "",
                "Dry run:",
                f"  {str(dry_run).lower()}",
            )
        )
    )


def command_run_per_item(config: ExperimentConfig, args: argparse.Namespace) -> int:
    """Run one complete same-task pair through native production ports."""

    task_id = str(args.task)
    if task_id not in FROZEN_TASKS:
        raise ProtocolError("run-per-item task is outside the frozen Community inventory")
    if config.per_item_reset is None:
        raise ProtocolError("run-per-item requires the canonical per-item reset protocol")
    dry_run = bool(args.dry_run or args.no_model_calls)
    baseline_only = bool(args.baseline_only)
    if baseline_only != bool(args.previous_closed_run_id):
        raise ProtocolError("--baseline-only requires exactly one --previous-closed-run-id")
    if baseline_only != bool(args.previous_closed_task):
        raise ProtocolError("--baseline-only requires exactly one --previous-closed-task")
    _print_per_item_runner_summary(
        config,
        task_id=task_id,
        run_id=args.run_id,
        dry_run=dry_run,
        baseline_only=baseline_only,
    )
    host_profile = managed_core_profile_readiness(config)
    output_root = config.experiment_root / "supervisor" / args.run_id
    planned_stages = (
        [
            "verify-previous-item-reset",
            "write-pre-dispatch-isolation-receipt",
            "fresh-generation-zero",
            "fresh-codex-baseline",
            "validate-and-seal-baseline",
            "stop-before-judge",
        ]
        if baseline_only
        else [
            "fresh-generation-zero",
            "fresh-codex-baseline",
            "baseline-judge",
            "sanitized-evaluator-feedback-projection",
            "baseline-success-trace",
            "baseline-achievement-ledger",
            "baseline-evidence-capsule",
            "feedback-admission",
            "native-evolve-once-preservation-first",
            "preservation-native-artifact-quality",
            "fresh-codex-evolved",
            "baseline-equivalence",
            "evolved-judge",
            "paired-result",
            "reset-active-state",
        ]
    )
    if dry_run:
        print_closed_json(
            {
                "status": "PER_ITEM_DRY_RUN_NO_MODEL_CALLS",
                "route_status": "planned_not_executed",
                "task_discovery": [task_id],
                "harness_route": (
                    "DurableTrainingControl -> ProductionTrainingOperations -> "
                    "CoreV2CandidatePort -> OpenEvo TaskRequest -> CodexHarness"
                ),
                "planned_stages": planned_stages,
                "output_root": str(output_root),
                "provider_calls": 0,
                "managed_runtime_ready": host_profile["ready"],
                "managed_runtime_reason_code": host_profile["reason_code"],
                "supervision": "preservation_first_evolution_r3",
                "judge_feedback_in_evolution": False,
                "judge_reasoning_in_evolution": False,
                "raw_gt_in_reflector": False,
                "sanitized_feedback_in_reflector": True,
                "baseline_evidence_capsule_in_reflector": True,
                "baseline_success_trace_in_reflector": True,
                "baseline_achievement_ledger_in_reflector": True,
                "gt_visible_to_candidate": False,
                "one_evolution_cycle": not baseline_only,
                "fresh_codex_per_pass": True,
                "reset_after_task": not baseline_only,
                "baseline_only": baseline_only,
                "direct_host_codex_subprocess": False,
                "secret_recorded": False,
            }
        )
        return 0
    if host_profile["ready"] is not True:
        print_closed_json(
            {
                "status": "BLOCKED_OPEN_EVO_HARNESS_RUNTIME",
                "task_id": task_id,
                "run_id": args.run_id,
                "reason_code": host_profile["reason_code"],
                "model_started": False,
                "provider_calls": 0,
                "secret_recorded": False,
            }
        )
        return 5
    # A live per-item pair is bound to the source trees and release assets in
    # the protocol.  ``run-per-item`` used to check only Docker readiness here,
    # which could create a new state namespace while the local adapter or Core
    # source had drifted from the pinned identities.  Keep this a no-provider
    # preflight and fail before any mutable control-plane operation.
    readiness = config.environment_readiness()
    if readiness.get("ready") is not True:
        print_closed_json(
            {
                "status": "BLOCKED_PER_ITEM_PRODUCTION_READINESS",
                "task_id": task_id,
                "run_id": args.run_id,
                "blockers": list(readiness.get("blockers", [])),
                "source_identity_matches": readiness.get("source_identity_matches"),
                "model_started": False,
                "provider_calls": 0,
                "secret_recorded": False,
            }
        )
        return 5
    if not baseline_only:
        judge = judge_credential_readiness(config)
        if judge.get("ready") is not True:
            print_closed_json(
                {
                    "status": "JUDGE_CREDENTIALS_REQUIRED",
                    "task_id": task_id,
                    "run_id": args.run_id,
                    "model_started": False,
                    "provider_calls": 0,
                    "secret_recorded": False,
                }
            )
            return 5
        judge_identity_preflight(
            config,
            receipt_path=(
                config.experiment_root
                / "per_item_preflight"
                / args.run_id
                / "judge-preflight.json"
            ),
        )
    state_path = output_root / "training-supervisor.sqlite3"
    if state_path.exists():
        print_closed_json(
            {
                "status": "BLOCKED_RUN_NAMESPACE_ALREADY_EXISTS",
                "task_id": task_id,
                "run_id": args.run_id,
                "model_started": False,
                "secret_recorded": False,
            }
        )
        return 5
    authority = acquire_managed_core_control(config)
    try:
        if baseline_only:
            previous = DurableTrainingControl(
                config=config,
                run_id=str(args.previous_closed_run_id),
                require_existing=True,
                task_ids=(str(args.previous_closed_task),),
            )
            previous_state = previous.status()
            previous_verification = previous.verify()
            previous_reset = previous_state.get("item_reset_receipt")
            if (
                previous_state.get("stage") != TrainingStage.ITEM_RESET.value
                or previous_state.get("active_artifact_ids") != []
                or previous_state.get("core_project_id") is not None
                or not isinstance(previous_reset, dict)
                or previous_reset.get("active_artifact_ids") != []
                or previous_reset.get("active_successor_head") is not None
                or previous_verification.get("status") != "PASS"
            ):
                raise ProtocolError("previous per-item state is not closed and reset")
            isolation_receipt = {
                "schema_version": ("openevo.researchclawbench.pre_dispatch_isolation.v1"),
                "task_id": task_id,
                "run_id": args.run_id,
                "previous_task_id": str(args.previous_closed_task),
                "previous_run_id": str(args.previous_closed_run_id),
                "previous_state_sha256": previous_state.get("_state_sha256"),
                "fresh_generation_zero": True,
                "active_artifact_ids": [],
                "active_successor_head": None,
                "active_project_head": None,
                "prior_task_gt_present": False,
                "prior_task_workspace_present": False,
                "prior_task_candidate_session_present": False,
                "cross_project_fork_from_previous_task": False,
                "task_provider_calls_before_receipt": 0,
                "secret_recorded": False,
            }
            _write(output_root / "pre_dispatch_isolation.json", isolation_receipt)
        control = DurableTrainingControl(
            config=config,
            run_id=args.run_id,
            require_existing=False,
            task_ids=(task_id,),
            production=True,
            core_control_authority=authority,
            current_task_gt_supervision=True,
            per_item_reset=True,
        )
        result = control.initialize()
        while result["stage"] not in _FORMAL_TERMINAL_STAGES and not (
            baseline_only and result["stage"] == TrainingStage.ARTIFACT_VALIDATED.value
        ):
            result = control.run_next()
    finally:
        authority.close()
    if baseline_only and result["stage"] == TrainingStage.ARTIFACT_VALIDATED.value:
        candidate = result.get("active_candidate_receipt")
        if not isinstance(candidate, dict):
            raise ProtocolError("baseline-only validation lacks Candidate receipt")
        print_closed_json(
            {
                "status": "PER_ITEM_NEXT_TASK_BASELINE_SEALED",
                "task_id": task_id,
                "run_id": args.run_id,
                "baseline_core_task_id": candidate.get("core_task_id"),
                "baseline_core_attempt_id": candidate.get("core_attempt_id"),
                "baseline_session_id": candidate.get("session_id"),
                "baseline_workspace": candidate.get("candidate_output_root"),
                "fresh_generation_zero": True,
                "active_artifact_ids": result.get("active_artifact_ids"),
                "judge_executed": False,
                "evolution_executed": False,
                "pre_dispatch_isolation_receipt": str(output_root / "pre_dispatch_isolation.json"),
            }
        )
        return 0
    if result["stage"] == TrainingStage.ITEM_RESET.value:
        verification = control.verify()
        print_closed_json(
            {
                "status": "PER_ITEM_FULL_CYCLE_CLOSED",
                "task_id": task_id,
                "run_id": args.run_id,
                "judge_executed": True,
                "evolved_candidate_executed": True,
                "one_native_evolution_cycle": True,
                "post_close_active_artifacts": result["active_artifact_ids"],
                "post_close_active_project_head": result["core_project_id"],
                "verification": verification,
                "state": result,
            }
        )
        return 0
    print_closed_json(result)
    return 5


def _per_item_subrun_id(run_id: str, index: int, task_id: str) -> str:
    suffix = f"_{index:02d}_{task_id}"
    candidate = f"{run_id}{suffix}"
    if len(candidate) > 105:
        raise ProtocolError("Community run ID is too long for per-item namespaces")
    return candidate


def _per_item_community_root(config: ExperimentConfig, run_id: str) -> Path:
    root = config.experiment_root / "per_item_community" / run_id
    resolved = root.resolve(strict=False)
    if not resolved.is_relative_to(
        (config.experiment_root / "per_item_community").resolve(strict=False)
    ):
        raise ProtocolError("per-item Community root escapes experiment authority")
    return resolved


def _per_item_community_plan(config: ExperimentConfig, run_id: str) -> list[dict[str, object]]:
    return [
        {
            "index": index,
            "task_id": task_id,
            "run_id": _per_item_subrun_id(run_id, index, task_id),
            "stages": [
                "baseline",
                "judge_baseline",
                "project_sanitized_evaluator_feedback",
                "admit_sanitized_evaluator_feedback",
                "build_baseline_success_trace",
                "build_baseline_achievement_ledger",
                "build_baseline_evidence_capsule",
                "evolve_once_preservation_first",
                "verify_preservation_native_artifacts",
                "evolved",
                "baseline_equivalence",
                "judge_evolved",
                "paired_result",
                "reset",
            ],
            "generation_zero": True,
            "cross_task_inheritance": False,
        }
        for index, task_id in enumerate(config.require("tasks"), start=1)
    ]


def _paired_statistics(
    items: list[dict[str, object]],
    *,
    failed: int = 0,
) -> dict[str, object]:
    pairs = [item["paired_result"] for item in items]
    baseline = [float(item["baseline_score"]) for item in pairs]
    evolved = [float(item["evolved_score"]) for item in pairs]
    deltas = [float(item["delta"]) for item in pairs]
    attempted = len(items) + failed
    return {
        "task_count": len(FROZEN_TASKS),
        "attempted": attempted,
        "paired_completed": len(items),
        "failed": failed,
        "baseline_mean": None if not baseline else sum(baseline) / len(baseline),
        "evolved_mean": None if not evolved else sum(evolved) / len(evolved),
        "paired_mean_delta": None if not deltas else sum(deltas) / len(deltas),
        "paired_median_delta": None if not deltas else statistics.median(deltas),
        "wins": sum(value > 0 for value in deltas),
        "ties": sum(value == 0 for value in deltas),
        "losses": sum(value < 0 for value in deltas),
        "candidate_failure_rate": 0.0 if not attempted else failed / attempted,
        "task_completion_rate": len(items) / len(FROZEN_TASKS),
    }


def command_run_per_item_community(config: ExperimentConfig, args: argparse.Namespace) -> int:
    """Run independent per-task controls; never carry an active head forward."""

    if config.per_item_reset is None:
        raise ProtocolError("Community command requires the per-item reset protocol")
    plan = _per_item_community_plan(config, args.run_id)
    root = _per_item_community_root(config, args.run_id)
    dry_run = bool(args.dry_run or args.no_model_calls)
    print(
        "\n".join(
            (
                "ResearchClawBench Per-Item Runner",
                "=================================",
                "",
                f"Protocol: {config.require('protocol_name')}",
                "Dataset: community",
                "Tasks: 17 canonical Community tasks",
                f"Run ID: {args.run_id}",
                "Protocol: baseline -> judge -> evolve once -> evolved -> judge -> pair -> reset",
                "Candidate: OpenEvo Core / CodexHarness / GPT-5.5 / managed",
                "Evolution: native successor / managed reflector / artifact registry",
                "Judge: OpenRouter / openai/gpt-5.1 / Azure",
                "Isolation: fresh Codex per pass; generation-zero per task; no cross-task artifacts",
                f"Output: {root}",
                f"Dry run: {str(dry_run).lower()}",
            )
        )
    )
    if dry_run:
        print_closed_json(
            {
                "status": "PER_ITEM_COMMUNITY17_DRY_RUN_NO_MODEL_CALLS",
                "protocol": config.require("protocol_name"),
                "task_discovery": list(config.require("tasks")),
                "plan": plan,
                "provider_calls": 0,
                "judge_calls": 0,
                "reflector_calls": 0,
                "direct_host_codex_subprocess": False,
                "output_root": str(root),
                "secret_recorded": False,
            }
        )
        return 0
    host_profile = managed_core_profile_readiness(config)
    if host_profile["ready"] is not True:
        print_closed_json(
            {
                "status": "BLOCKED_OPEN_EVO_HARNESS_RUNTIME",
                "run_id": args.run_id,
                "reason_code": host_profile["reason_code"],
                "model_started": False,
                "provider_calls": 0,
                "secret_recorded": False,
            }
        )
        return 5
    judge = judge_credential_readiness(config)
    if judge.get("ready") is not True:
        print_closed_json(
            {
                "status": "JUDGE_CREDENTIALS_REQUIRED",
                "run_id": args.run_id,
                "model_started": False,
                "provider_calls": 0,
                "secret_recorded": False,
            }
        )
        return 5
    judge_identity_preflight(
        config,
        receipt_path=(
            config.experiment_root / "per_item_preflight" / args.run_id / "judge-preflight.json"
        ),
    )
    if root.exists():
        raise ProtocolError("per-item Community run namespace already exists")
    root.mkdir(parents=True, mode=0o700)
    authority = acquire_managed_core_control(config)
    completed: list[dict[str, object]] = []
    previous_reset: dict[str, object] | None = None
    try:
        for item in plan:
            task_id = str(item["task_id"])
            item_run_id = str(item["run_id"])
            item_root = root / "items" / f"{int(item['index']):02d}_{task_id}"
            item_root.mkdir(parents=True, mode=0o700)
            isolation = {
                "schema_version": ("openevo.researchclawbench.pre_dispatch_isolation.v1"),
                "task_id": task_id,
                "run_id": item_run_id,
                "fresh_generation_zero": True,
                "active_artifact_ids": [],
                "active_successor_head": None,
                "active_project_head": None,
                "prior_task_gt": None,
                "prior_task_workspace": None,
                "prior_task_candidate_session": None,
                "cross_project_fork_from_previous_task": False,
                "previous_item_reset": previous_reset,
                "task_provider_calls_before_receipt": 0,
                "secret_recorded": False,
            }
            _write(item_root / "pre_dispatch_isolation.json", isolation)
            control = DurableTrainingControl(
                config=config,
                run_id=item_run_id,
                require_existing=False,
                task_ids=(task_id,),
                production=True,
                core_control_authority=authority,
                current_task_gt_supervision=True,
                per_item_reset=True,
            )
            result = control.initialize()
            while result["stage"] not in _FORMAL_TERMINAL_STAGES:
                result = control.run_next()
            if result["stage"] != TrainingStage.ITEM_RESET.value:
                failed = {
                    "task_id": task_id,
                    "run_id": item_run_id,
                    "stage": result["stage"],
                    "state_sha256": result.get("_state_sha256"),
                }
                _write(
                    root / "progress.json",
                    {
                        "status": "PER_ITEM_COMMUNITY17_STOPPED_ON_ITEM_FAILURE",
                        "completed": completed,
                        "failure": failed,
                        "statistics": _paired_statistics(completed, failed=1),
                        "silent_skip": False,
                        "cross_task_continuation_after_failure": False,
                    },
                )
                return 5
            verification = control.verify()
            closed = {
                "task_id": task_id,
                "run_id": item_run_id,
                "paired_result": result["paired_result"],
                "reset_receipt": result["item_reset_receipt"],
                "verification": verification,
            }
            completed.append(closed)
            previous_reset = {
                "task_id": task_id,
                "run_id": item_run_id,
                "active_artifact_ids": [],
                "active_successor_head": None,
                "state_sha256": result["_state_sha256"],
            }
            _write(
                root / "progress.json",
                {
                    "status": "PER_ITEM_COMMUNITY17_RUNNING",
                    "completed": completed,
                    "next_task_index": int(item["index"]) + 1,
                },
            )
    finally:
        authority.close()
    summary = {
        "status": "PER_ITEM_COMMUNITY17_CLOSED",
        "run_id": args.run_id,
        "items": completed,
        "statistics": _paired_statistics(completed),
    }
    _write(root / "paired_results.json", summary)
    _write(root / "progress.json", summary)
    print_closed_json(summary)
    return 0


def command_per_item_community_inspect(config: ExperimentConfig, args: argparse.Namespace) -> int:
    root = _per_item_community_root(config, args.run_id)
    progress = root / "progress.json"
    if not progress.is_file():
        raise ProtocolError("per-item Community progress does not exist")
    payload = json.loads(progress.read_text(encoding="utf-8"))
    if args.command == "per-item-community-status":
        print_closed_json(payload)
        return 0
    completed = payload.get("items", payload.get("completed", []))
    if not isinstance(completed, list):
        raise ProtocolError("per-item Community progress inventory is invalid")
    failure = payload.get("failure")
    inventory = list(completed)
    if isinstance(failure, dict):
        inventory.append(failure)
    results = []
    for item in inventory:
        control = DurableTrainingControl(
            config=config,
            run_id=str(item["run_id"]),
            require_existing=True,
            task_ids=(str(item["task_id"]),),
        )
        if args.command == "per-item-community-stop-owned":
            results.append(control.stop_owned())
        else:
            results.append(control.verify())
    print_closed_json(
        {
            "status": "PASS",
            "command": args.command,
            "run_id": args.run_id,
            "items": results,
        }
    )
    return 0


def command_recover_native_evolution(
    config: ExperimentConfig,
    args: argparse.Namespace,
) -> int:
    """Resume only the failed Life_005 successor through Core recovery APIs."""

    dry_run = bool(args.dry_run or args.no_model_calls)
    print(
        "\n".join(
            (
                "ResearchClawBench Native Evolution Recovery",
                "===========================================",
                "",
                "Task: Life_005",
                f"Source Run ID: {args.source_run_id}",
                f"Recovery Run ID: {args.run_id}",
                "Candidate: sealed source only; not re-executed",
                "Evolution: OpenEvo native successor / managed reflector",
                "Supervision: current-task GT; Judge feedback excluded",
                "Targets: agent-system, skill, memory",
                "Judge: not executed",
                "Evolved Candidate: not executed",
                f"Dry run: {str(dry_run).lower()}",
            )
        )
    )
    if dry_run:
        print_closed_json(
            native_evolution_recovery_dry_run(
                config,
                source_run_id=args.source_run_id,
                recovery_run_id=args.run_id,
            )
        )
        return 0
    authority = acquire_managed_core_control(config)
    try:
        adapter = NativeEvolutionRecoveryAdapter(
            config=config,
            source_run_id=args.source_run_id,
            recovery_run_id=args.run_id,
            core_authority=authority,
        )
        if args.prepare_only:
            prepared = adapter.prepare()
            print_closed_json(
                {
                    "status": "NATIVE_EVOLUTION_RECOVERY_PREPARED_NO_MODEL_CALLS",
                    "task_id": prepared["task_id"],
                    "source_run_id": prepared["source_run_id"],
                    "recovery_run_id": prepared["recovery_run_id"],
                    "source_state_sha256": prepared["supervisor_state_sha256"],
                    "source_tree_sha256": prepared["supervisor_tree_sha256"],
                    "execution_identity": prepared["execution_identity"],
                    "provider_calls": 0,
                    "model_started": False,
                    "output_root": str(adapter.recovery_root),
                }
            )
            return 0
        if args.create_only:
            print_closed_json(adapter.create())
            return 0
        result = adapter.run()
        print_closed_json(result)
        return 0
    finally:
        authority.close()


def command_pilot_v2(args: argparse.Namespace) -> int:
    """Run the three-task per-item pilot v2 (dry-run or live)."""

    config = PilotV2Config.load(args.config)
    output_root = config.output_root
    output_root.mkdir(parents=True, exist_ok=True)
    dry_run = bool(args.dry_run or config.dry_run)
    if dry_run:
        candidate_port = PilotV2DryRunCandidate()
        evolution_port = PilotV2DryRunEvolution()
        judge_port = PilotV2DryRunJudge()
    else:
        candidate_port = DeepSeekCodexEngineeringPort(
            model=str(config.require("candidate.model")),
            timeout_seconds=int(config.require("candidate.timeout_seconds")),
            sandbox=str(config.require("candidate.sandbox")),
        )
        evolution_port = DeepSeekCodexEngineeringPort(
            model=str(config.require("evolution.model")),
            timeout_seconds=int(config.require("evolution.timeout_seconds")),
        )
        judge_port = PilotV2JudgePort(
            researchclawbench_root=config.researchclawbench_root,
            evaluator_private_root=(output_root / "evaluator_private" / args.batch_id),
            timeout_seconds=int(config.require("judge.timeout_seconds")),
        )
    state_root = output_root / "supervisor" / args.batch_id
    fresh = not (state_root / "training-supervisor.sqlite3").is_file()
    runner = PilotV2Runner(
        config=config,
        batch_id=args.batch_id,
        state_root=state_root,
        candidate_port=candidate_port,
        evolution_port=evolution_port,
        judge_port=judge_port,
        dry_run=dry_run,
        adopt_from=args.adopt_batch,
        mark_nondelivery=tuple(args.mark_nondelivery or ()),
    )
    if fresh:
        runner.initialize()
    elif args.adopt_batch is not None:
        raise PilotV2Error(
            "ADOPT_BATCH_EXISTS",
            "an adopted continuation batch must start from a fresh run ID",
        )
    state = runner.run_until_terminal()
    print_closed_json(state)
    if state.get("stage") == "PILOT_CLOSED":
        return 0
    if state.get("stage") == "PILOT_BUDGET_EXHAUSTED":
        return 6
    return 4


def command_delivery_canary_v3(args: argparse.Namespace) -> int:
    """Run the Protocol v3 baseline-only delivery reliability canary."""

    config = DeliveryCanaryConfig.load(args.config)
    output_root = config.output_root
    output_root.mkdir(parents=True, exist_ok=True)
    dry_run = bool(args.dry_run or config.dry_run)
    if dry_run:
        candidate_port = DeliveryCanaryDryRunCandidate()
    else:
        provider = str(config.require("candidate.provider"))
        if provider == "native_codex":
            host_profile = managed_core_host_profile_readiness()
            print_closed_json(
                {
                    "status": (
                        "BLOCKED_OPEN_EVO_HARNESS_RUNTIME"
                        if host_profile["ready"] is not True
                        else "BLOCKED_OPEN_EVO_HARNESS_WIRING"
                    ),
                    "reason_code": (
                        host_profile["reason_code"]
                        if host_profile["ready"] is not True
                        else "DIRECT_CODEX_SUBPROCESS_ROUTE_REJECTED"
                    ),
                    "model": str(config.require("candidate.model")),
                    "model_started": False,
                    "provider_calls": 0,
                    "secret_recorded": False,
                }
            )
            return 5
        else:
            candidate_port = DeepSeekCodexEngineeringPort(
                model=str(config.require("candidate.model")),
                timeout_seconds=int(config.require("candidate.timeout_seconds")),
                sandbox=str(config.require("candidate.sandbox")),
            )
    state_root = output_root / "supervisor" / args.batch_id
    fresh = not (state_root / "training-supervisor.sqlite3").is_file()
    runner = DeliveryCanaryV3Runner(
        config=config,
        batch_id=args.batch_id,
        state_root=state_root,
        candidate_port=candidate_port,
        dry_run=dry_run,
    )
    if fresh:
        runner.initialize()
    state = runner.run_until_terminal()
    print_closed_json(state)
    if state.get("stage") == "CANARY_CLOSED":
        return 0
    return 7


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
    reconciliation = sub.add_parser("reconcile-candidate-sealed")
    reconciliation.add_argument("--protocol", required=True, type=Path)
    reconciliation.add_argument("--spec", required=True, type=Path)
    terminal_reconciliation = sub.add_parser("reconcile-validator-terminal")
    terminal_reconciliation.add_argument("--protocol", required=True, type=Path)
    terminal_reconciliation.add_argument("--spec", required=True, type=Path)
    validator_learning = sub.add_parser("validator-learning-init")
    validator_learning.add_argument("--protocol", required=True, type=Path)
    validator_learning.add_argument("--spec", required=True, type=Path)
    validator_learning_run = sub.add_parser("validator-learning-run")
    validator_learning_run.add_argument("--protocol", required=True, type=Path)
    validator_learning_run.add_argument("--run-id", required=True)
    validator_learning_replace = sub.add_parser("validator-learning-replace-pre-effect")
    validator_learning_replace.add_argument("--protocol", required=True, type=Path)
    validator_learning_replace.add_argument("--spec", required=True, type=Path)
    validator_learning_post_feedback_replace = sub.add_parser(
        "validator-learning-replace-post-feedback"
    )
    validator_learning_post_feedback_replace.add_argument("--protocol", required=True, type=Path)
    validator_learning_post_feedback_replace.add_argument("--spec", required=True, type=Path)
    formal_prepare = sub.add_parser("formal-v11-prepare")
    formal_prepare.add_argument("--protocol", required=True, type=Path)
    formal_prepare.add_argument("--output", required=True, type=Path)
    formal_prepare.add_argument("--identity-output", required=True, type=Path)
    formal_prepare.add_argument("--community-run-id", required=True)
    formal_prepare.add_argument("--official-run-id", required=True)
    formal_prepare.add_argument("--framework-lock", required=True, type=Path)
    formal_prepare.add_argument("--daemon-bundle", required=True, type=Path)
    formal_prepare.add_argument("--daemon-manifest", required=True, type=Path)
    formal_prepare.add_argument("--reflector-readiness-receipt", required=True, type=Path)
    formal_prepare.add_argument(
        "--reflector-credential-mount-readiness-receipt",
        required=True,
        type=Path,
    )
    formal_prepare.add_argument("--evaluator-dependency-lock", required=True, type=Path)
    formal_validate = sub.add_parser("formal-community-validate")
    formal_validate.add_argument("--protocol", required=True, type=Path)
    formal_validate.add_argument("--run-id", required=True)
    for name in (
        "formal-community-init",
        "formal-community-start",
        "formal-community-status",
        "formal-community-verify",
        "formal-community-run-next",
        "formal-community-resume",
        "formal-community-stop-owned",
    ):
        control = sub.add_parser(name)
        control.add_argument("--protocol", required=True, type=Path)
        control.add_argument("--run-id", required=True)
        if name in {
            "formal-community-start",
            "formal-community-run-next",
            "formal-community-resume",
        }:
            control.add_argument("--until", choices=tuple(item.value for item in TrainingStage))
    for name in (
        "official-frozen-validate",
        "official-frozen-init",
        "official-frozen-start",
        "official-frozen-status",
        "official-frozen-verify",
        "official-frozen-run-next",
        "official-frozen-resume",
        "official-frozen-stop-owned",
    ):
        control = sub.add_parser(name)
        control.add_argument("--protocol", required=True, type=Path)
        control.add_argument("--community-run-id", required=True)
        control.add_argument("--run-id", required=True)
    for name in (
        "training-init",
        "training-start",
        "status",
        "verify",
        "run-next",
        "resume",
        "stop-owned",
    ):
        control = sub.add_parser(name)
        control.add_argument("--protocol", required=True, type=Path)
        control.add_argument("--run-id", required=True)
        if name in {"training-init", "training-start"}:
            control.add_argument(
                "--scope",
                choices=("life-005", "community-17"),
                default="community-17",
            )
        if name in {"training-start", "run-next", "resume"}:
            control.add_argument("--until", choices=tuple(item.value for item in TrainingStage))
    per_item_invalidate = sub.add_parser("per-item-invalidate-undispatched-evolution")
    per_item_invalidate.add_argument("--protocol", required=True, type=Path)
    per_item_invalidate.add_argument("--run-id", required=True)
    per_item_invalidate.add_argument(
        "--reason",
        required=True,
        choices=("MECHANISM_CHANGE_AFTER_TASK_3",),
    )
    minimal = sub.add_parser("minimal-per-item")
    minimal.add_argument("--protocol", required=True, type=Path)
    minimal.add_argument("--run-id", required=True)
    minimal.add_argument("--dry-run", action="store_true")
    minimal.add_argument("--no-model-calls", action="store_true")
    run_per_item = sub.add_parser("run-per-item")
    run_per_item.add_argument("--protocol", required=True, type=Path)
    run_per_item.add_argument("--run-id", required=True)
    run_per_item.add_argument("--task", required=True, choices=FROZEN_TASKS)
    run_per_item.add_argument("--dry-run", action="store_true")
    run_per_item.add_argument("--no-model-calls", action="store_true")
    run_per_item.add_argument("--baseline-only", action="store_true")
    run_per_item.add_argument("--previous-closed-run-id")
    run_per_item.add_argument("--previous-closed-task", choices=FROZEN_TASKS)
    per_item_community = sub.add_parser("run-per-item-community")
    per_item_community.add_argument("--protocol", required=True, type=Path)
    per_item_community.add_argument("--run-id", required=True)
    per_item_community.add_argument("--dry-run", action="store_true")
    per_item_community.add_argument("--no-model-calls", action="store_true")
    for name in (
        "per-item-community-status",
        "per-item-community-verify",
        "per-item-community-stop-owned",
    ):
        command = sub.add_parser(name)
        command.add_argument("--protocol", required=True, type=Path)
        command.add_argument("--run-id", required=True)
    native_recovery = sub.add_parser("recover-native-evolution")
    native_recovery.add_argument("--protocol", required=True, type=Path)
    native_recovery.add_argument("--source-run-id", required=True)
    native_recovery.add_argument("--run-id", required=True)
    native_recovery_mode = native_recovery.add_mutually_exclusive_group()
    native_recovery_mode.add_argument("--prepare-only", action="store_true")
    native_recovery_mode.add_argument("--create-only", action="store_true")
    native_recovery.add_argument("--dry-run", action="store_true")
    native_recovery.add_argument("--no-model-calls", action="store_true")
    judge_probe = sub.add_parser("judge-probe")
    judge_probe.add_argument("--project-root", required=True, type=Path)
    judge_probe.add_argument("--output-root", required=True, type=Path)
    judge_probe.add_argument("--expected-judge-model", required=True)
    judge_probe.add_argument("--expected-judge-api-base", required=True)
    judge_env_diagnostic = sub.add_parser("judge-env-diagnostic")
    judge_env_diagnostic.add_argument("--project-root", required=True, type=Path)
    judge_env_diagnostic.add_argument("--output-root", required=True, type=Path)
    rejudge = sub.add_parser("rejudge-sealed")
    rejudge.add_argument("--experiment-root", required=True, type=Path)
    rejudge.add_argument("--source-run-id", required=True)
    rejudge.add_argument("--rejudge-id", required=True)
    rejudge.add_argument("--researchclawbench-root", required=True, type=Path)
    pilot_v2 = sub.add_parser("pilot-v2")
    pilot_v2.add_argument("--config", required=True, type=Path)
    pilot_v2.add_argument("--batch-id", required=True)
    pilot_v2.add_argument("--adopt-batch", default=None)
    pilot_v2.add_argument("--mark-nondelivery", action="append", default=[])
    pilot_v2.add_argument("--dry-run", action="store_true")
    delivery_canary_v3 = sub.add_parser("delivery-canary-v3")
    delivery_canary_v3.add_argument("--config", required=True, type=Path)
    delivery_canary_v3.add_argument("--batch-id", required=True)
    delivery_canary_v3.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    core_control_authority = None
    try:
        if args.command == "minimal-per-item":
            return command_minimal_per_item(args)
        if args.command == "judge-probe":
            result = run_judge_probe_subprocess(
                project_root=args.project_root,
                output_root=args.output_root,
                expected_model=args.expected_judge_model,
                expected_api_base=args.expected_judge_api_base,
            )
            print_closed_json(result)
            return 0 if result.get("status") == "JUDGE_SUBPROCESS_OK" else 4
        if args.command == "judge-env-diagnostic":
            result = run_judge_environment_diagnostic(
                project_root=args.project_root,
                output_root=args.output_root,
            )
            print_closed_json(result)
            return 0
        if args.command == "rejudge-sealed":
            result = run_rejudge_sealed(
                experiment_root=args.experiment_root,
                source_run_id=args.source_run_id,
                rejudge_id=args.rejudge_id,
                researchclawbench_root=args.researchclawbench_root,
            )
            print_closed_json(result)
            return 0 if result.get("status") == "REJUDGE_SEALED_CLOSED" else 4
        if args.command == "pilot-v2":
            return command_pilot_v2(args)
        if args.command == "delivery-canary-v3":
            return command_delivery_canary_v3(args)
        config = ExperimentConfig.load(args.protocol)
        if args.command == "run-per-item":
            return command_run_per_item(config, args)
        if args.command == "run-per-item-community":
            return command_run_per_item_community(config, args)
        if args.command in {
            "per-item-community-status",
            "per-item-community-verify",
            "per-item-community-stop-owned",
        }:
            return command_per_item_community_inspect(config, args)
        if args.command == "recover-native-evolution":
            return command_recover_native_evolution(config, args)
        if args.command == "per-item-invalidate-undispatched-evolution":
            control = DurableTrainingControl(
                config=config,
                run_id=args.run_id,
                require_existing=True,
                production=False,
            )
            result = control.invalidate_undispatched_per_item_evolution(reason=args.reason)
            print_closed_json(result)
            return 0
        if getattr(config, "formal_runs_v11", None) is not None and args.command in {
            "training-start",
            "run-next",
            "resume",
        }:
            raise ProtocolError(
                "formal v11 mutations require the formal-community command surface"
            )
        if args.command == "readiness":
            return command_readiness(config)
        if args.command == "static-audit":
            return command_static_audit(config)
        if args.command == "contamination-audit":
            return command_contamination_audit(config)
        if args.command == "native-canary":
            return command_native_canary(config, args.task, args.attempt)
        if args.command == "formal-v11-prepare":
            receipt = prepare_formal_v11_protocol(
                base_config=config,
                output_path=args.output,
                identity_path=args.identity_output,
                community_run_id=args.community_run_id,
                official_run_id=args.official_run_id,
                runtime_assets=FormalV11RuntimeAssetOverrides(
                    framework_lock=args.framework_lock,
                    daemon_bundle=args.daemon_bundle,
                    daemon_manifest=args.daemon_manifest,
                    reflector_readiness_receipt=args.reflector_readiness_receipt,
                    reflector_credential_mount_readiness_receipt=(
                        args.reflector_credential_mount_readiness_receipt
                    ),
                    evaluator_dependency_lock=args.evaluator_dependency_lock,
                ),
            )
            print_closed_json(receipt)
            return 0
        if args.command == "formal-community-validate":
            result = command_formal_community_validate(config, args.run_id)
            print_closed_json(result)
            return 0 if result["ready"] else 5
        if args.command == "official-frozen-validate":
            authority = acquire_managed_core_control(config)
            try:
                bind_formal_core_identity(
                    config=config,
                    run_id=args.run_id,
                    authority_readiness=authority.public_readiness(),
                )
                result = validate_official_control(
                    config=config,
                    community_run_id=args.community_run_id,
                    official_run_id=args.run_id,
                    core_authority=authority,
                )
            finally:
                authority.close()
            print_closed_json(result)
            return 0 if result["mutating_run_ready"] else 5
        formal_community_commands = {
            "formal-community-init",
            "formal-community-start",
            "formal-community-status",
            "formal-community-verify",
            "formal-community-run-next",
            "formal-community-resume",
            "formal-community-stop-owned",
        }
        official_commands = {
            "official-frozen-init",
            "official-frozen-start",
            "official-frozen-status",
            "official-frozen-verify",
            "official-frozen-run-next",
            "official-frozen-resume",
            "official-frozen-stop-owned",
        }
        mutating = args.command in {
            "training-start",
            "run-next",
            "resume",
            "reconcile-candidate-sealed",
            "reconcile-validator-terminal",
            "validator-learning-init",
            "validator-learning-run",
            "validator-learning-replace-pre-effect",
            "validator-learning-replace-post-feedback",
        }
        core_control_authority = acquire_managed_core_control(config) if mutating else None
        if (
            mutating
            and core_control_authority is not None
            and not core_control_authority.managed_host_profile_ready
        ):
            raise ManagedCoreControlUnavailable(
                "formal training requires docker_user_container_v1 host profile"
            )
        if args.command in formal_community_commands:
            formal = config.formal_runs_v11
            if formal is None or formal["community"]["run_id"] != args.run_id:
                raise ProtocolError("formal Community run ID differs from the v11 protocol")
            validate_formal_v11_identity(config)
            if args.command in {"formal-community-init", "formal-community-start"}:
                preflight = command_formal_community_live_preflight(
                    config,
                    args.run_id,
                )
                if not preflight["ready"]:
                    print_closed_json(preflight)
                    return 5
            if args.command in {"formal-community-init", "formal-community-start"}:
                control = DurableTrainingControl(
                    config=config,
                    run_id=args.run_id,
                    require_existing=False,
                    task_ids=FROZEN_TASKS,
                    production=False,
                )
                result = control.initialize()
                if args.command == "formal-community-start":
                    until = args.until or TrainingStage.FINAL_FROZEN.value
                    while result["stage"] != until:
                        if result["stage"] in _FORMAL_TERMINAL_STAGES:
                            break
                        result = _run_formal_community_transition(
                            config=config,
                            run_id=args.run_id,
                            operation="run_next",
                        )
            elif args.command == "formal-community-status":
                control = DurableTrainingControl(
                    config=config,
                    run_id=args.run_id,
                    require_existing=True,
                    task_ids=FROZEN_TASKS,
                    production=False,
                )
                result = control.status()
            elif args.command == "formal-community-verify":
                control = DurableTrainingControl(
                    config=config,
                    run_id=args.run_id,
                    require_existing=True,
                    task_ids=FROZEN_TASKS,
                    production=False,
                )
                result = control.verify()
            elif args.command == "formal-community-stop-owned":
                control = DurableTrainingControl(
                    config=config,
                    run_id=args.run_id,
                    require_existing=True,
                    task_ids=FROZEN_TASKS,
                    production=False,
                )
                result = control.stop_owned()
            elif args.command == "formal-community-run-next":
                result = _run_formal_community_transition(
                    config=config,
                    run_id=args.run_id,
                    operation="run_next",
                )
                while args.until and result["stage"] != args.until:
                    if result["stage"] in _FORMAL_TERMINAL_STAGES:
                        break
                    result = _run_formal_community_transition(
                        config=config,
                        run_id=args.run_id,
                        operation="run_next",
                    )
            else:
                result = _run_formal_community_transition(
                    config=config,
                    run_id=args.run_id,
                    operation="resume",
                )
                while args.until and result["stage"] != args.until:
                    if result["stage"] in _FORMAL_TERMINAL_STAGES:
                        break
                    result = _run_formal_community_transition(
                        config=config,
                        run_id=args.run_id,
                        operation="resume",
                    )
            print_closed_json(result)
            return (
                5
                if result.get("stage")
                in {
                    TrainingStage.BUDGET_EXHAUSTED.value,
                    TrainingStage.BLOCKED.value,
                    TrainingStage.FAILED.value,
                    TrainingStage.TASK_NO_VALID_ATTEMPT.value,
                    TrainingStage.CANDIDATE_SETUP_BLOCKED.value,
                }
                else 0
            )
        if args.command in official_commands:
            if args.command in {"official-frozen-init", "official-frozen-start"}:
                control = OfficialTrainingControl(
                    config=config,
                    community_run_id=args.community_run_id,
                    official_run_id=args.run_id,
                    production=False,
                    require_existing=False,
                )
                result = control.initialize()
                if args.command == "official-frozen-start":
                    while result["stage"] not in _OFFICIAL_TERMINAL_STAGES:
                        result = _run_formal_official_transition(
                            config=config,
                            community_run_id=args.community_run_id,
                            official_run_id=args.run_id,
                            operation="run_next",
                        )
            elif args.command == "official-frozen-status":
                control = OfficialTrainingControl(
                    config=config,
                    community_run_id=args.community_run_id,
                    official_run_id=args.run_id,
                    production=False,
                    require_existing=True,
                )
                result = control.status()
            elif args.command == "official-frozen-verify":
                control = OfficialTrainingControl(
                    config=config,
                    community_run_id=args.community_run_id,
                    official_run_id=args.run_id,
                    production=False,
                    require_existing=True,
                )
                result = control.verify()
            elif args.command == "official-frozen-stop-owned":
                control = OfficialTrainingControl(
                    config=config,
                    community_run_id=args.community_run_id,
                    official_run_id=args.run_id,
                    production=False,
                    require_existing=True,
                )
                result = control.stop_owned()
            elif args.command == "official-frozen-run-next":
                result = _run_formal_official_transition(
                    config=config,
                    community_run_id=args.community_run_id,
                    official_run_id=args.run_id,
                    operation="run_next",
                )
            else:
                result = _run_formal_official_transition(
                    config=config,
                    community_run_id=args.community_run_id,
                    official_run_id=args.run_id,
                    operation="resume",
                )
            print_closed_json(result)
            return 5 if result.get("stage") in {"BLOCKED", "FAILED"} else 0
        if args.command == "reconcile-candidate-sealed":
            if core_control_authority is None:
                raise TrainingOperationsUnavailable(
                    "managed Core authority is required for reconciliation"
                )
            spec = CandidateReconciliationSpec.load(args.spec)
            result = reconcile_candidate_sealed_authority(
                config=config,
                spec=spec,
                core_authority=core_control_authority,
            )
            print_closed_json(result)
            return 0
        if args.command == "reconcile-validator-terminal":
            if core_control_authority is None:
                raise TrainingOperationsUnavailable(
                    "managed Core authority is required for terminal reconciliation"
                )
            spec = ValidatorTerminalReconciliationSpec.load(args.spec)
            result = reconcile_invalid_validator_terminal(
                config=config,
                spec=spec,
                core_authority=core_control_authority,
            )
            print_closed_json(result)
            return 0
        if args.command == "validator-learning-init":
            if core_control_authority is None:
                raise TrainingOperationsUnavailable(
                    "managed Core authority is required for validator learning"
                )
            spec = ValidatorFailureLearningSpec.load(args.spec)
            result = initialize_validator_failure_learning(
                config=config,
                spec=spec,
                core_authority=core_control_authority,
            )
            print_closed_json(result)
            return 0
        if args.command == "validator-learning-replace-pre-effect":
            if core_control_authority is None:
                raise TrainingOperationsUnavailable(
                    "managed Core authority is required for validator-learning replacement"
                )
            spec = ValidatorLearningPreEffectReplacementSpec.load(args.spec)
            result = replace_validator_learning_pre_effect_namespace(
                config=config,
                spec=spec,
                core_authority=core_control_authority,
            )
            print_closed_json(result)
            return 0
        if args.command == "validator-learning-replace-post-feedback":
            if core_control_authority is None:
                raise TrainingOperationsUnavailable(
                    "managed Core authority is required for validator-learning replacement"
                )
            spec = ValidatorLearningPreEffectReplacementSpec.load(args.spec)
            result = replace_validator_learning_post_feedback_pre_evolution_namespace(
                config=config,
                spec=spec,
                core_authority=core_control_authority,
            )
            print_closed_json(result)
            return 0
        if args.command == "validator-learning-run":
            if config.community_validator_failure_policy is None:
                raise ProtocolError("validator-learning policy is not active")
            control = DurableTrainingControl(
                config=config,
                run_id=args.run_id,
                require_existing=True,
                task_ids=("Astronomy_004",),
                production=True,
                core_control_authority=core_control_authority,
            )
            result = control.status()
            terminal = {
                TrainingStage.CROSS_TASK_SANITIZED.value,
                TrainingStage.TASK_NO_VALID_ATTEMPT.value,
            }
            while result["stage"] not in terminal:
                result = control.resume()
            print_closed_json(result)
            return 0
        scope = getattr(args, "scope", None)
        task_ids = (
            (CANARY_TASK,)
            if scope == "life-005"
            else (FROZEN_TASKS if scope == "community-17" else None)
        )
        control = DurableTrainingControl(
            config=config,
            run_id=args.run_id,
            require_existing=args.command not in {"training-init", "training-start"},
            task_ids=task_ids,
            production=mutating,
            core_control_authority=core_control_authority,
        )
        if args.command in {"training-init", "training-start"}:
            result = control.initialize()
            if args.command == "training-start":
                until = args.until or TrainingStage.FINAL_FROZEN.value
                while result["stage"] != until:
                    result = control.run_next()
        elif args.command == "status":
            result = control.status()
        elif args.command == "verify":
            result = control.verify()
        elif args.command == "stop-owned":
            result = control.stop_owned()
        elif args.command == "run-next":
            result = control.run_next()
            if args.until:
                while result["stage"] != args.until:
                    result = control.run_next()
        else:
            result = control.resume()
            if args.until:
                while result["stage"] != args.until:
                    result = control.resume()
        print_closed_json(result)
        return 0
    except RejudgeSealedError as exc:
        print(
            json.dumps(
                {
                    "status": "REJUDGE_SEALED_BLOCKED",
                    "failure_code": exc.code,
                    "message": str(exc),
                },
                indent=2,
            )
        )
        return 4
    except JudgeCredentialsRequired as exc:
        print(
            json.dumps(
                {
                    "status": "JUDGE_CREDENTIALS_REQUIRED",
                    "message": str(exc),
                    # The missing Judge boundary can be reached after a
                    # Candidate has already sealed.  The generic exception
                    # surface has no authority to claim that no paid model ran;
                    # callers must read the durable supervisor/operation
                    # receipts for that fact.
                    "model_started": None,
                    "model_started_status": "UNKNOWN_REQUIRES_DURABLE_RECEIPT",
                },
                indent=2,
            )
        )
        return 3
    except (
        ProtocolError,
        OSError,
        ValueError,
        RuntimeError,
        TrainingOperationsUnavailable,
        OfficialTrainingOperationsUnavailable,
    ) as exc:
        print(
            json.dumps(
                {
                    "status": "FAILED_CLOSED",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    # A transport exception may be observed after a durable
                    # external side effect.  Never turn absence of an in-band
                    # response into a false no-model assertion.
                    "model_started": None,
                    "model_started_status": "UNKNOWN_REQUIRES_DURABLE_RECEIPT",
                },
                indent=2,
            )
        )
        return 2
    finally:
        if core_control_authority is not None:
            core_control_authority.close()


if __name__ == "__main__":
    raise SystemExit(main())
