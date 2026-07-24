"""Fail-closed CLI wiring for the paired taskwise online experiment."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from openevo.evolution.framework import (
    canonical_digest,
    load_verified_framework_registry,
)
from openevo_chembench.chembench4k_dataset import ChemBench4KDatasetLoader
from openevo_chembench.chembench4k_models import CHEMBENCH4K_CATEGORIES
from openevo_chembench.chembench4k_prompt import render_official_five_shot_prompt
from openevo_chembench.local_codex_executor import LocalCodexCLIExecutor
from openevo_chembench.taskwise_config_v1 import (
    SOURCE_COMMIT_PLACEHOLDER,
    TaskwiseExperimentConfigV1,
    load_taskwise_config_v1,
    taskwise_arm_parity_findings,
)
from openevo_chembench.taskwise_core_evolution_v1 import (
    TaskwiseCoreEvolutionBridgeV1,
    TaskwiseCoreUpdatePortAdapterV1,
)
from openevo_chembench.taskwise_online_runner_v1 import (
    TaskwiseCoreUpdatePortV1,
    TaskwiseEpisodeV1,
    TaskwiseExecutorV1,
    TaskwiseMemoryPublicMetricsV1,
    TaskwiseOnlineRunnerV1,
    TaskwiseRunConfigV1,
    TaskwiseRunStatusV1,
    build_taskwise_run_binding_v1,
    load_private_taskwise_results_v1,
)
from openevo_chembench.taskwise_reporting_v1 import (
    PROTOCOL_LABELS,
    TaskwiseCanaryEvidenceV1,
    build_taskwise_canary9_report_v1,
    build_taskwise_online_report_v1,
)
from openevo_chembench.taskwise_sampling_v1 import (
    PILOT500_STREAM_COUNT,
    PILOT500_STREAM_SCOPES,
    PILOT500_TASKS_PER_STREAM,
    TaskwiseManifestSet,
    select_taskwise_stream,
    verify_taskwise_manifests,
)
from openevo_chembench.taskwise_stream_statistics_v1 import (
    PrivateTaskwisePairedStreamV1,
    build_taskwise_pilot500_stream_report_v1,
)
from openevo_chembench.reflector_execution_boundary_v2 import (
    ReflectorExecutionBoundaryV2,
    TASKWISE_SOURCE_SPLIT,
)
from openevo_chembench.source_identity_v2 import (
    SourceManifestError,
    verify_source_manifest,
)


_MODULE = Path(__file__).resolve()
PACKAGE_ROOT = _MODULE.parents[2]
REPOSITORY_ROOT = _MODULE.parents[4]
WORKSPACE_ROOT = _MODULE.parents[5]
CONFIG_ROOT = PACKAGE_ROOT / "configs"
TASKWISE_STATE_ROOT = PACKAGE_ROOT / "state" / "taskwise_online_v1"
FRAMEWORK_LOCK = PACKAGE_ROOT / "state" / "v2" / "framework" / "framework-lock.json"
SOURCE_MANIFEST = PACKAGE_ROOT / "manifests" / "chembench_source_manifest_v2.json"
_SCOPES = ("canary9", "pilot500", *PILOT500_STREAM_SCOPES)
_PRIMARY_SCOPES = ("canary9", *PILOT500_STREAM_SCOPES)
_SUITE_RUN_STATUSES = frozenset({"COMPLETED", "INCOMPLETE", "FAILED"})
_TERMINAL_SECURITY_STATUSES = frozenset(
    {
        "SECURITY_TOOL_USE_VIOLATION",
        "TASKWISE_EVOLUTION_UPDATE_FAILED",
        "TASKWISE_CONTEXT_BINDING_VIOLATION",
    }
)
_ARTIFACT_FAILURE_CODES = frozenset(
    {
        "ARTIFACT_VALIDATION_FAILED",
        "TASKWISE_ARTIFACT_VALIDATION_FAILED",
    }
)


class TaskwiseCLIError(RuntimeError):
    """Closed CLI preflight/dispatch failure."""


ExecutorFactoryV1 = Callable[
    [TaskwiseExperimentConfigV1],
    TaskwiseExecutorV1,
]
CorePortFactoryV1 = Callable[
    [TaskwiseExperimentConfigV1],
    TaskwiseCoreUpdatePortV1,
]
SourceGateV1 = Callable[[TaskwiseExperimentConfigV1], str]


def run_arm(
    config_path: Path,
    *,
    resume: bool = False,
    executor_factory: ExecutorFactoryV1 | None = None,
    core_port_factory: CorePortFactoryV1 | None = None,
    source_gate: SourceGateV1 | None = None,
) -> dict[str, object]:
    """Validate every frozen input before constructing paid runtime objects."""

    config = load_taskwise_config_v1(config_path.resolve())
    gate = source_gate or verify_taskwise_source_gate_v1
    current_commit = gate(config)
    loader, manifest, paired_configs = _verify_static_inputs(config, config_path)
    episodes = _build_episodes(loader, scope=config.scope)
    if len(episodes) != manifest.item_count:
        raise TaskwiseCLIError("TASKWISE_EPISODE_COUNT_MISMATCH")

    output_directory = _resolve_workspace_path(
        config.output_directory,
        field_name="output_directory",
        must_exist=False,
    )
    if resume and not output_directory.is_dir():
        raise TaskwiseCLIError("TASKWISE_RESUME_OUTPUT_MISSING")
    if not resume and output_directory.exists():
        raise TaskwiseCLIError("TASKWISE_OUTPUT_TARGET_EXISTS")
    selected_executor_factory = executor_factory or build_default_taskwise_executor_v1
    selected_core_factory = core_port_factory or build_default_taskwise_core_port_v1
    executor = selected_executor_factory(config)
    core_port: TaskwiseCoreUpdatePortV1 | None = None
    try:
        core_port = None if config.arm == "control" else selected_core_factory(config)
        runner = TaskwiseOnlineRunnerV1(
            config=_build_run_config(
                config=config,
                output_directory=output_directory,
                paired_configs=paired_configs,
                current_commit=current_commit,
                dataset_sha256=loader.manifest.combined_sha256,
                task_manifest_sha256=manifest.public_sha256,
            ),
            episodes=episodes,
            executor=executor,
            core_update_port=core_port,
            resume=resume,
        )
        result = runner.run()
    finally:
        close_core = getattr(core_port, "close", None)
        if callable(close_core):
            close_core()
        close = getattr(executor, "close", None)
        if callable(close):
            close()
    return result.to_public_dict()


def run_pilot500_stream_suite(
    *,
    arm: str,
    arm_runner: Callable[..., dict[str, object]] | None = None,
    suite_state_path: Path | None = None,
    source_gate: SourceGateV1 | None = None,
) -> dict[str, object]:
    """Start, resume, or skip each isolated stream from its public run state."""

    if arm not in {"control", "online"}:
        raise TaskwiseCLIError("TASKWISE_SUITE_ARM_INVALID")
    runner = arm_runner or run_arm
    state_path = suite_state_path or _resolve_workspace_path(
        (
            "OpenEvo/results/chembench4k_taskwise_online_v1/"
            f"pilot500_streams/{arm}_suite_state.json"
        ),
        field_name="pilot500 suite state",
        must_exist=False,
    )
    state = _load_or_initialize_suite_state(state_path, arm=arm)
    if state["status"] == "FAILED":
        raise TaskwiseCLIError("TASKWISE_SUITE_TERMINAL_FAILURE")

    gate = source_gate or verify_taskwise_source_gate_v1
    for scope in PILOT500_STREAM_SCOPES:
        config_path = CONFIG_ROOT / f"{arm}_{scope}_taskwise_online_v1.yaml"
        config = load_taskwise_config_v1(config_path)
        gate(config)
        _verify_static_inputs(config, config_path)
        output = _resolve_workspace_path(
            config.output_directory,
            field_name="stream output_directory",
            must_exist=False,
        )
        action, evidence = _suite_stream_action(output, expected_arm=arm)
        if action == "skip":
            state["streams"][scope] = {
                "action": "SKIPPED_COMPLETED",
                **evidence,
            }
            _update_suite_totals(state)
            _write_suite_state(state_path, state)
            continue
        if action == "terminal":
            state["streams"][scope] = {
                "action": "TERMINAL_FAILURE",
                **evidence,
            }
            state["status"] = "FAILED"
            _update_suite_totals(state)
            _write_suite_state(state_path, state)
            raise TaskwiseCLIError("TASKWISE_SUITE_TERMINAL_FAILURE")

        resume = action == "resume"
        state["status"] = "INCOMPLETE"
        state["streams"][scope] = {
            "action": "RESUMING" if resume else "STARTING",
            **evidence,
        }
        _update_suite_totals(state)
        _write_suite_state(state_path, state)
        try:
            result = runner(config_path, resume=resume)
        except Exception:
            state["streams"][scope] = {
                "action": "ORCHESTRATION_ERROR",
                "run_status": "EXECUTION_FAILED",
                "resume_allowed": False,
                "failure_code": "SUITE_ORCHESTRATION_ERROR",
                "completed": False,
                "missing": False,
            }
            state["status"] = "FAILED"
            _update_suite_totals(state)
            _write_suite_state(state_path, state)
            raise
        result_evidence = _suite_result_evidence(result)
        state["streams"][scope] = {
            "action": "RESUMED" if resume else "STARTED",
            **result_evidence,
        }
        if result_evidence["completed"]:
            state["streams"][scope]["action"] = "COMPLETED"
            _update_suite_totals(state)
            _write_suite_state(state_path, state)
            continue
        state["status"] = (
            "FAILED"
            if result_evidence["run_status"] in _TERMINAL_SECURITY_STATUSES
            or not result_evidence["resume_allowed"]
            else "INCOMPLETE"
        )
        _update_suite_totals(state)
        _write_suite_state(state_path, state)
        if state["status"] == "FAILED":
            raise TaskwiseCLIError("TASKWISE_SUITE_TERMINAL_FAILURE")
        return state

    state["status"] = "COMPLETED"
    _update_suite_totals(state)
    _write_suite_state(state_path, state)
    return state


def dry_run() -> dict[str, object]:
    """Recompute configs, parity, dataset, and manifests without paid objects."""

    scopes: dict[str, object] = {}
    for scope in _PRIMARY_SCOPES:
        control_path = CONFIG_ROOT / f"control_{scope}_taskwise_online_v1.yaml"
        control = load_taskwise_config_v1(control_path)
        loader, manifest, paired = _verify_static_inputs(control, control_path)
        scopes[scope] = {
            "item_count": manifest.item_count,
            "dataset_sha256": loader.manifest.combined_sha256,
            "public_manifest_sha256": manifest.public_sha256,
            "private_manifest_sha256": manifest.private_sha256,
            "ordered_uid_sha256": manifest.ordered_uid_sha256,
            "protocol_sha256": _protocol_sha256(paired),
            "parity_findings": [],
        }
    return {
        "schema_version": "taskwise_online_dry_run_v1",
        "status": "PASS",
        "protocol_labels": list(PROTOCOL_LABELS),
        "model_calls": 0,
        "executor_instantiated": False,
        "core_port_instantiated": False,
        "pilot500_stream_design": {
            "stream_count": PILOT500_STREAM_COUNT,
            "tasks_per_stream": PILOT500_TASKS_PER_STREAM,
            "total_item_count": (PILOT500_STREAM_COUNT * PILOT500_TASKS_PER_STREAM),
            "generation_zero_reset_between_streams": True,
            "legacy_single_chain_scope": "pilot500",
        },
        "scopes": scopes,
    }


def compare(
    *,
    scope: str,
    persist: bool = True,
    source_gate: SourceGateV1 | None = None,
) -> dict[str, Any]:
    """Build the private non-standard report from two completed paired arms."""

    if scope not in _SCOPES:
        raise TaskwiseCLIError("TASKWISE_SCOPE_INVALID")
    control_path = CONFIG_ROOT / f"control_{scope}_taskwise_online_v1.yaml"
    control = load_taskwise_config_v1(control_path)
    gate = source_gate or verify_taskwise_source_gate_v1
    current_commit = gate(control)
    loader, manifest, paired = _verify_static_inputs(control, control_path)
    online = paired["online"]
    control_output = _resolve_workspace_path(
        control.output_directory,
        field_name="control output_directory",
        must_exist=True,
    )
    online_output = _resolve_workspace_path(
        online.output_directory,
        field_name="online output_directory",
        must_exist=True,
    )
    control_state = _completed_run_state(control_output, expected_arm="control")
    online_state = _completed_run_state(online_output, expected_arm="online")
    episodes = _build_episodes(loader, scope=scope)
    expected_bindings = {
        arm: build_taskwise_run_binding_v1(
            _build_run_config(
                config=arm_config,
                output_directory=(control_output if arm == "control" else online_output),
                paired_configs=paired,
                current_commit=current_commit,
                dataset_sha256=loader.manifest.combined_sha256,
                task_manifest_sha256=manifest.public_sha256,
            ),
            episodes,
        )
        for arm, arm_config in paired.items()
    }
    if (
        control_state.get("binding") != expected_bindings["control"]
        or online_state.get("binding") != expected_bindings["online"]
    ):
        raise TaskwiseCLIError("TASKWISE_PAIRED_RUN_BINDING_MISMATCH")

    control_rows = load_private_taskwise_results_v1(control_output)
    online_rows = load_private_taskwise_results_v1(online_output)
    if scope == "canary9":
        report = build_taskwise_canary9_report_v1(
            control=control_rows,
            online=online_rows,
            evidence=TaskwiseCanaryEvidenceV1(
                control_completion_count=_strict_state_counter(
                    control_state,
                    "completion_count",
                ),
                online_completion_count=_strict_state_counter(
                    online_state,
                    "completion_count",
                ),
                control_core_job_count=_strict_state_counter(
                    control_state,
                    "core_job_count",
                ),
                control_core_artifact_count=_strict_state_counter(
                    control_state,
                    "core_artifact_count",
                ),
                online_core_job_count=_strict_state_counter(
                    online_state,
                    "core_job_count",
                ),
                online_core_artifact_count=_strict_state_counter(
                    online_state,
                    "core_artifact_count",
                ),
                control_context_binding_violations=_strict_state_counter(
                    control_state,
                    "context_binding_violation_count",
                ),
                online_context_binding_violations=_strict_state_counter(
                    online_state,
                    "context_binding_violation_count",
                ),
                control_security_violations=_security_violation_count(control_state),
                online_security_violations=_security_violation_count(online_state),
                online_artifact_validation_failures=(
                    _artifact_validation_failure_count(online_state)
                ),
            ),
        )
    else:
        report = build_taskwise_online_report_v1(
            control=control_rows,
            online=online_rows,
            control_security_violations=_security_violation_count(control_state),
            online_security_violations=_security_violation_count(online_state),
            online_artifact_validation_failures=(_artifact_validation_failure_count(online_state)),
        )
    if persist:
        path = (
            _resolve_workspace_path(
                f"OpenEvo/results/chembench4k_taskwise_online_v1/{scope}",
                field_name="comparison output root",
                must_exist=True,
            )
            / "private"
            / "taskwise_comparison_v1.json"
        )
        path.parent.mkdir(mode=0o700, exist_ok=True)
        if path.exists():
            raise TaskwiseCLIError("TASKWISE_COMPARISON_OUTPUT_EXISTS")
        payload = _canonical_bytes(report)
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    return report


def compare_pilot500_streams(
    *,
    persist: bool = True,
    source_gate: SourceGateV1 | None = None,
) -> dict[str, Any]:
    """Aggregate ten separately executed paired streams without joining memory."""

    run_evidence = _pilot_stream_run_evidence(source_gate=source_gate)
    if any(not item["completed"] for item in run_evidence):
        report = _incomplete_pilot_stream_report(run_evidence)
        if persist:
            _persist_pilot_stream_report(report)
        return report

    streams: list[PrivateTaskwisePairedStreamV1] = []
    for scope in PILOT500_STREAM_SCOPES:
        paired_report = compare(
            scope=scope,
            persist=False,
            source_gate=source_gate,
        )
        if paired_report["task_count"] != PILOT500_TASKS_PER_STREAM:
            raise TaskwiseCLIError("TASKWISE_STREAM_TASK_COUNT_MISMATCH")
        control_path = CONFIG_ROOT / f"control_{scope}_taskwise_online_v1.yaml"
        control = load_taskwise_config_v1(control_path)
        gate = source_gate or verify_taskwise_source_gate_v1
        gate(control)
        _loader, _manifest, paired = _verify_static_inputs(control, control_path)
        online = paired["online"]
        control_output = _resolve_workspace_path(
            control.output_directory,
            field_name="control output_directory",
            must_exist=True,
        )
        online_output = _resolve_workspace_path(
            online.output_directory,
            field_name="online output_directory",
            must_exist=True,
        )
        control_state = _completed_run_state(control_output, expected_arm="control")
        online_state = _completed_run_state(online_output, expected_arm="online")
        control_memory_metrics, control_context_findings = _stream_public_evidence(
            control_output,
            expected_arm="control",
            state=control_state,
        )
        online_memory_metrics, online_context_findings = _stream_public_evidence(
            online_output,
            expected_arm="online",
            state=online_state,
        )
        if control_memory_metrics:
            raise TaskwiseCLIError("TASKWISE_CONTROL_MEMORY_EVIDENCE_INVALID")
        streams.append(
            PrivateTaskwisePairedStreamV1(
                stream_id=scope,
                control=load_private_taskwise_results_v1(control_output),
                online=load_private_taskwise_results_v1(online_output),
                control_security_violations=_security_violation_count(control_state),
                online_security_violations=_security_violation_count(online_state),
                online_artifact_validation_failures=(
                    _artifact_validation_failure_count(online_state)
                ),
                context_binding_violations=(control_context_findings + online_context_findings),
                completed=(
                    control_state["status"] == "COMPLETED"
                    and online_state["status"] == "COMPLETED"
                ),
                approved_memory_utf8_bytes=online_memory_metrics,
            )
        )
    report = build_taskwise_pilot500_stream_report_v1(tuple(streams))
    if persist:
        _persist_pilot_stream_report(report)
    return report


def _pilot_stream_run_evidence(
    *,
    source_gate: SourceGateV1 | None,
) -> tuple[dict[str, object], ...]:
    """Collect only public run-state evidence before any private result read."""

    gate = source_gate or verify_taskwise_source_gate_v1
    evidence: list[dict[str, object]] = []
    for scope in PILOT500_STREAM_SCOPES:
        for arm in ("control", "online"):
            config_path = CONFIG_ROOT / f"{arm}_{scope}_taskwise_online_v1.yaml"
            config = load_taskwise_config_v1(config_path)
            gate(config)
            _verify_static_inputs(config, config_path)
            output = _resolve_workspace_path(
                config.output_directory,
                field_name=f"{arm} output_directory",
                must_exist=False,
            )
            if not output.exists():
                evidence.append(
                    {
                        "stream_id": scope,
                        "arm": arm,
                        "run_status": "MISSING",
                        "resume_allowed": False,
                        "failure_code": None,
                        "completed": False,
                        "missing": True,
                        "context_binding_violations": 0,
                    }
                )
                continue
            try:
                state = _read_public_run_state(output, expected_arm=arm)
                item = _suite_state_evidence(state)
                context_count = _strict_state_counter(
                    state,
                    "context_binding_violation_count",
                )
            except TaskwiseCLIError:
                item = {
                    "run_status": "EXECUTION_FAILED",
                    "resume_allowed": False,
                    "failure_code": "RUN_STATE_UNAVAILABLE",
                    "completed": False,
                    "missing": False,
                }
                context_count = 0
            evidence.append(
                {
                    "stream_id": scope,
                    "arm": arm,
                    **item,
                    "context_binding_violations": context_count,
                }
            )
    return tuple(evidence)


def _incomplete_pilot_stream_report(
    evidence: tuple[dict[str, object], ...],
) -> dict[str, Any]:
    if len(evidence) != PILOT500_STREAM_COUNT * 2:
        raise TaskwiseCLIError("TASKWISE_SUITE_EVIDENCE_INVALID")
    grouped: dict[str, dict[str, dict[str, object]]] = {
        scope: {} for scope in PILOT500_STREAM_SCOPES
    }
    for item in evidence:
        stream_id = item.get("stream_id")
        arm = item.get("arm")
        if stream_id not in grouped or arm not in {"control", "online"}:
            raise TaskwiseCLIError("TASKWISE_SUITE_EVIDENCE_INVALID")
        grouped[stream_id][arm] = item
    if any(set(pair) != {"control", "online"} for pair in grouped.values()):
        raise TaskwiseCLIError("TASKWISE_SUITE_EVIDENCE_INVALID")

    infrastructure_failures = 0
    security_violations = 0
    evolution_update_failures = 0
    artifact_validation_failures = 0
    context_binding_violations = 0
    terminal = False
    per_stream: list[dict[str, object]] = []
    for scope in PILOT500_STREAM_SCOPES:
        pair = grouped[scope]
        pair_completed = all(pair[arm]["completed"] is True for arm in ("control", "online"))
        stream_payload: dict[str, object] = {
            "stream_id": scope,
            "completed": pair_completed,
        }
        for arm in ("control", "online"):
            item = pair[arm]
            failure_class = _stream_failure_class(item)
            missing = item.get("missing") is True
            if missing or failure_class == "infrastructure":
                infrastructure_failures += 1
            elif failure_class == "security":
                security_violations += 1
                terminal = True
            elif failure_class == "evolution_update":
                evolution_update_failures += 1
                terminal = True
            elif failure_class == "artifact":
                artifact_validation_failures += 1
                terminal = True
            elif failure_class == "context":
                terminal = True
            context_count = item.get("context_binding_violations")
            if isinstance(context_count, bool) or not isinstance(context_count, int):
                raise TaskwiseCLIError("TASKWISE_SUITE_EVIDENCE_INVALID")
            context_binding_violations += context_count
            terminal = terminal or context_count > 0
            if (
                failure_class == "infrastructure"
                and not missing
                and item.get("resume_allowed") is not True
            ):
                terminal = True
            stream_payload[f"{arm}_status"] = item.get("run_status")
            stream_payload[f"{arm}_resume_allowed"] = item.get("resume_allowed") is True
            stream_payload[f"{arm}_failure_code"] = item.get("failure_code")
        per_stream.append(stream_payload)

    completed_streams = sum(item["completed"] is True for item in per_stream)
    return {
        "schema_version": "taskwise_online_pilot500_ten_stream_report_v1",
        "protocol_id": "taskwise_online_evolution_v1",
        "protocol_labels": list(PROTOCOL_LABELS),
        "standard_chembench4k_score_claimed": False,
        "status": "FAILED" if terminal else "INCOMPLETE",
        "decision": "NO_GO",
        "stream_count": PILOT500_STREAM_COUNT,
        "tasks_per_stream": PILOT500_TASKS_PER_STREAM,
        "task_count": PILOT500_STREAM_COUNT * PILOT500_TASKS_PER_STREAM,
        "completed_streams": completed_streams,
        "per_stream": per_stream,
        "private_results_read": False,
        "infrastructure_failures": infrastructure_failures,
        "security_violations": security_violations,
        "evolution_update_failures": evolution_update_failures,
        "artifact_validation_failures": artifact_validation_failures,
        "context_binding_violations": context_binding_violations,
        "go_gate": {
            "completed_streams_required": PILOT500_STREAM_COUNT,
            "security_violations_required": 0,
            "artifact_validation_failures_required": 0,
            "context_binding_violations_required": 0,
            "mean_stream_delta_minimum": 0.02,
            "stream_block_bootstrap_ci_lower_must_exceed": 0.0,
            "positive_stream_count_minimum": 7,
        },
    }


def _persist_pilot_stream_report(report: dict[str, Any]) -> None:
    root = _resolve_workspace_path(
        "OpenEvo/results/chembench4k_taskwise_online_v1/pilot500_streams",
        field_name="pilot500 stream comparison output root",
        must_exist=False,
    )
    path = root / "private" / "taskwise_stream_comparison_v1.json"
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.exists():
        raise TaskwiseCLIError("TASKWISE_COMPARISON_OUTPUT_EXISTS")
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(_canonical_bytes(report))
        stream.flush()
        os.fsync(stream.fileno())


def verify_taskwise_source_gate_v1(config: TaskwiseExperimentConfigV1) -> str:
    """Bind a paid arm to current HEAD and a clean benchmark package."""

    if type(config) is not TaskwiseExperimentConfigV1:
        raise TypeError("config must be exact TaskwiseExperimentConfigV1")
    head = _git("rev-parse", "HEAD").strip()
    if config.source_commit not in {SOURCE_COMMIT_PLACEHOLDER, head}:
        raise TaskwiseCLIError("TASKWISE_SOURCE_COMMIT_MISMATCH")
    package_status = _git(
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--",
        "benchmarks/chembench",
    )
    if package_status.strip():
        raise TaskwiseCLIError("TASKWISE_PACKAGE_SOURCE_DIRTY")
    try:
        verify_source_manifest(PACKAGE_ROOT, SOURCE_MANIFEST)
    except (OSError, ValueError, SourceManifestError) as exc:
        raise TaskwiseCLIError("TASKWISE_SOURCE_MANIFEST_MISMATCH") from exc
    return head


def build_default_taskwise_executor_v1(
    config: TaskwiseExperimentConfigV1,
) -> TaskwiseExecutorV1:
    """Construct the real zero-tool Local Codex executor after all gates."""

    return LocalCodexCLIExecutor(
        config=config,
        task_timeout_seconds=config.executor.timeout_seconds,
        diagnostic_root=(
            PACKAGE_ROOT
            / "state"
            / "taskwise_online_v1"
            / "private_executor_events"
            / config.scope
            / config.arm
        ),
    )


def build_default_taskwise_core_port_v1(
    config: TaskwiseExperimentConfigV1,
) -> TaskwiseCoreUpdatePortV1:
    """Construct the registered-method Core port after no-model preflight."""

    if type(config) is not TaskwiseExperimentConfigV1 or config.arm != "online":
        raise TypeError("default Core port requires the online taskwise config")
    if not FRAMEWORK_LOCK.is_file():
        raise TaskwiseCLIError("TASKWISE_VERIFIED_FRAMEWORK_LOCK_MISSING")
    capability = ReflectorExecutionBoundaryV2.detect_capability()
    if not capability.available:
        raise TaskwiseCLIError("REFLECTOR_FILESYSTEM_ISOLATION_MISSING")
    state_root = TASKWISE_STATE_ROOT / config.scope / config.run_name
    output_root = _resolve_workspace_path(
        config.output_directory,
        field_name="output_directory",
        must_exist=False,
    )
    if output_root.exists() != state_root.exists():
        raise TaskwiseCLIError("TASKWISE_CORE_RUN_STATE_MISMATCH")
    if not state_root.exists():
        state_root.mkdir(parents=True, mode=0o700)
    state_root.chmod(0o700)
    private_audit_root = state_root / "private_reflector_events"

    # Validate the real Codex/auth/bubblewrap inputs before the first task call.
    probe_record = {
        "uid": "0" * 64,
        "source_split": TASKWISE_SOURCE_SPLIT,
    }
    probe_digest = canonical_digest([probe_record])
    with tempfile.TemporaryDirectory(
        prefix=".taskwise-reflector-preflight-",
        dir=state_root,
    ) as temporary:
        probe_path = Path(temporary) / "records.jsonl"
        probe_path.write_bytes(_canonical_bytes(probe_record))
        probe_path.chmod(0o600)
        boundary = ReflectorExecutionBoundaryV2(
            dev_artifact_path=probe_path,
            expected_records_sha256=probe_digest,
            expected_record_count=1,
            expected_source_split=TASKWISE_SOURCE_SPLIT,
            private_audit_root=private_audit_root,
            timeout_seconds=config.executor.timeout_seconds,
        )
        if not boundary.preflight().available:
            raise TaskwiseCLIError("REFLECTOR_FILESYSTEM_ISOLATION_MISSING")

    def boundary_factory(
        artifact_path: Path,
        records_sha256: str,
        record_count: int,
    ) -> ReflectorExecutionBoundaryV2:
        return ReflectorExecutionBoundaryV2(
            dev_artifact_path=artifact_path,
            expected_records_sha256=records_sha256,
            expected_record_count=record_count,
            expected_source_split=TASKWISE_SOURCE_SPLIT,
            private_audit_root=private_audit_root,
            timeout_seconds=config.executor.timeout_seconds,
        )

    bridge = TaskwiseCoreEvolutionBridgeV1(
        db_path=state_root / "evolution.sqlite3",
        artifact_root=state_root / "artifacts",
        executable_registry=load_verified_framework_registry(FRAMEWORK_LOCK),
        reflector_boundary_factory=boundary_factory,
        checkpoint_path=state_root / "private_lineage_checkpoints.jsonl",
        memory_limits=config.memory_limits,
    )
    return TaskwiseCoreUpdatePortAdapterV1(bridge)


def _verify_static_inputs(
    config: TaskwiseExperimentConfigV1,
    config_path: Path,
) -> tuple[
    ChemBench4KDatasetLoader,
    TaskwiseManifestSet,
    dict[str, TaskwiseExperimentConfigV1],
]:
    if type(config) is not TaskwiseExperimentConfigV1:
        raise TypeError("config must be exact TaskwiseExperimentConfigV1")
    paired_paths = {
        arm: config_path.resolve().with_name(f"{arm}_{config.scope}_taskwise_online_v1.yaml")
        for arm in ("control", "online")
    }
    try:
        paired = {arm: load_taskwise_config_v1(path) for arm, path in paired_paths.items()}
    except (OSError, ValueError) as exc:
        raise TaskwiseCLIError("TASKWISE_PAIRED_CONFIG_UNAVAILABLE") from exc
    if taskwise_arm_parity_findings(paired["control"], paired["online"]):
        raise TaskwiseCLIError("TASKWISE_ARM_PARITY_MISMATCH")
    if config != paired[config.arm]:
        raise TaskwiseCLIError("TASKWISE_CONFIG_PATH_BINDING_MISMATCH")
    snapshot_root = _resolve_workspace_path(
        config.dataset_root,
        field_name="dataset_root",
        must_exist=True,
    )
    dataset_manifest = _resolve_workspace_path(
        config.dataset_manifest,
        field_name="dataset_manifest",
        must_exist=True,
    )
    loader = ChemBench4KDatasetLoader(
        snapshot_root=snapshot_root,
        manifest_path=dataset_manifest,
    )
    public_path = _resolve_workspace_path(
        config.task_manifest,
        field_name="task_manifest",
        must_exist=True,
    )
    private_path = _resolve_workspace_path(
        config.private_task_manifest,
        field_name="private_task_manifest",
        must_exist=True,
    )
    summary_path = public_path.with_name(
        public_path.name.replace("_public_manifest.jsonl", "_summary.json")
    )
    manifest = verify_taskwise_manifests(
        loader,
        scope=config.scope,
        public_path=public_path,
        private_path=private_path,
        summary_path=summary_path,
    )
    return loader, manifest, paired


def _build_episodes(
    loader: ChemBench4KDatasetLoader,
    *,
    scope: str,
) -> tuple[TaskwiseEpisodeV1, ...]:
    tasks, _allocation, _seed = select_taskwise_stream(loader, scope=scope)
    dev = {
        category: loader.load_category(category, split="dev")
        for category in CHEMBENCH4K_CATEGORIES
    }
    return tuple(
        TaskwiseEpisodeV1(
            task=task,
            prompt=render_official_five_shot_prompt(
                task.to_public(),
                category_dev=dev[task.category],
            ),
        )
        for task in tasks
    )


def _build_run_config(
    *,
    config: TaskwiseExperimentConfigV1,
    output_directory: Path,
    paired_configs: dict[str, TaskwiseExperimentConfigV1],
    current_commit: str,
    dataset_sha256: str,
    task_manifest_sha256: str,
) -> TaskwiseRunConfigV1:
    return TaskwiseRunConfigV1(
        arm=config.arm,
        run_id=config.run_name,
        output_directory=output_directory,
        protocol_sha256=_protocol_sha256(paired_configs),
        source_commit=current_commit,
        dataset_sha256=dataset_sha256,
        task_manifest_sha256=task_manifest_sha256,
        model_identity_sha256=_model_identity_sha256(config),
        executor_policy_sha256=_executor_policy_sha256(config),
        memory_limits=config.memory_limits,
    )


def _protocol_sha256(
    paired: dict[str, TaskwiseExperimentConfigV1],
) -> str:
    return hashlib.sha256(
        _canonical_bytes(
            {
                "schema_version": "taskwise_paired_protocol_binding_v1",
                "control_config_sha256": paired["control"].config_sha256(),
                "online_config_sha256": paired["online"].config_sha256(),
                "protocol_labels": list(PROTOCOL_LABELS),
            }
        )
    ).hexdigest()


def _model_identity_sha256(config: TaskwiseExperimentConfigV1) -> str:
    return hashlib.sha256(
        _canonical_bytes(
            {
                "model": config.model,
                "reasoning_effort": config.reasoning_effort,
                "codex_cli_version": config.codex_cli_version,
                "prompt_renderer_id": config.prompt_renderer_id,
                "parser_id": config.parser_id,
                "evaluator_id": config.evaluator_id,
            }
        )
    ).hexdigest()


def _executor_policy_sha256(config: TaskwiseExperimentConfigV1) -> str:
    return hashlib.sha256(_canonical_bytes(config.to_payload()["executor"])).hexdigest()


def _load_or_initialize_suite_state(path: Path, *, arm: str) -> dict[str, Any]:
    if not path.exists():
        return {
            "schema_version": "taskwise_pilot500_stream_suite_state_v1",
            "arm": arm,
            "status": "INCOMPLETE",
            "stream_count": PILOT500_STREAM_COUNT,
            "tasks_per_stream": PILOT500_TASKS_PER_STREAM,
            "reset_memory_between_streams": True,
            "completed_streams": 0,
            "infrastructure_failures": 0,
            "security_violations": 0,
            "evolution_update_failures": 0,
            "artifact_validation_failures": 0,
            "context_binding_violations": 0,
            "streams": {
                scope: {
                    "action": "NOT_STARTED",
                    "run_status": "MISSING",
                    "resume_allowed": False,
                    "failure_code": None,
                    "completed": False,
                    "missing": True,
                }
                for scope in PILOT500_STREAM_SCOPES
            },
        }
    try:
        raw = path.read_bytes()
        state = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TaskwiseCLIError("TASKWISE_SUITE_STATE_INVALID") from exc
    if (
        type(state) is not dict
        or raw != _canonical_bytes(state)
        or state.get("schema_version") != "taskwise_pilot500_stream_suite_state_v1"
        or state.get("arm") != arm
        or state.get("status") not in _SUITE_RUN_STATUSES
        or state.get("stream_count") != PILOT500_STREAM_COUNT
        or state.get("tasks_per_stream") != PILOT500_TASKS_PER_STREAM
        or state.get("reset_memory_between_streams") is not True
        or type(state.get("streams")) is not dict
        or tuple(state["streams"]) != PILOT500_STREAM_SCOPES
    ):
        raise TaskwiseCLIError("TASKWISE_SUITE_STATE_INVALID")
    return state


def _write_suite_state(path: Path, state: dict[str, Any]) -> None:
    if state.get("status") not in _SUITE_RUN_STATUSES:
        raise TaskwiseCLIError("TASKWISE_SUITE_STATE_INVALID")
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.parent.chmod(0o700)
    payload = _canonical_bytes(state)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_public_run_state(path: Path, *, expected_arm: str) -> dict[str, Any]:
    try:
        raw = (path / "run_state.json").read_bytes()
        state = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TaskwiseCLIError("TASKWISE_RUN_STATE_UNAVAILABLE") from exc
    if (
        type(state) is not dict
        or raw != _canonical_bytes(state)
        or state.get("schema_version") != "taskwise_online_run_state_v1"
        or state.get("arm") != expected_arm
        or state.get("status") not in {item.value for item in TaskwiseRunStatusV1}
        or state.get("standard_chembench4k_score_claimed") is not False
    ):
        raise TaskwiseCLIError("TASKWISE_RUN_STATE_INVALID")
    return state


def _suite_stream_action(
    output: Path,
    *,
    expected_arm: str,
) -> tuple[str, dict[str, object]]:
    if not output.exists():
        return (
            "start",
            {
                "run_status": "MISSING",
                "resume_allowed": False,
                "failure_code": None,
                "completed": False,
                "missing": True,
            },
        )
    try:
        state = _read_public_run_state(output, expected_arm=expected_arm)
    except TaskwiseCLIError:
        return (
            "terminal",
            {
                "run_status": "EXECUTION_FAILED",
                "resume_allowed": False,
                "failure_code": "RUN_STATE_UNAVAILABLE",
                "completed": False,
                "missing": False,
            },
        )
    evidence = _suite_state_evidence(state)
    status = evidence["run_status"]
    if status == TaskwiseRunStatusV1.COMPLETED.value:
        return "skip", evidence
    if status in _TERMINAL_SECURITY_STATUSES:
        return "terminal", evidence
    pending = state.get("pending_invocation")
    safely_checkpointed = (
        status == TaskwiseRunStatusV1.RUNNING.value
        and pending is None
        and state.get("resume_allowed") is False
    )
    explicitly_resumable = (
        status == TaskwiseRunStatusV1.EXECUTION_FAILED.value
        and evidence["resume_allowed"] is True
        and type(pending) is dict
        and pending.get("completion_observed") is False
    )
    if safely_checkpointed or explicitly_resumable:
        return "resume", evidence
    return "terminal", evidence


def _suite_state_evidence(state: dict[str, Any]) -> dict[str, object]:
    failure = state.get("failure")
    failure_code = failure.get("code") if type(failure) is dict else None
    status = state.get("status")
    return {
        "run_status": status,
        "resume_allowed": state.get("resume_allowed") is True,
        "failure_code": failure_code if type(failure_code) is str else None,
        "completed": status == TaskwiseRunStatusV1.COMPLETED.value,
        "missing": False,
    }


def _suite_result_evidence(result: object) -> dict[str, object]:
    if type(result) is not dict:
        raise TaskwiseCLIError("TASKWISE_SUITE_RESULT_INVALID")
    status = result.get("status")
    if status not in {item.value for item in TaskwiseRunStatusV1}:
        raise TaskwiseCLIError("TASKWISE_SUITE_RESULT_INVALID")
    findings = result.get("finding_codes")
    failure_code = (
        findings[0]
        if type(findings) is list and len(findings) == 1 and type(findings[0]) is str
        else None
    )
    return {
        "run_status": status,
        "resume_allowed": result.get("resume_allowed") is True,
        "failure_code": failure_code,
        "completed": status == TaskwiseRunStatusV1.COMPLETED.value,
        "missing": False,
    }


def _update_suite_totals(state: dict[str, Any]) -> None:
    entries = tuple(state["streams"].values())
    state["completed_streams"] = sum(entry.get("completed") is True for entry in entries)
    state["infrastructure_failures"] = sum(
        _stream_failure_class(entry) == "infrastructure" for entry in entries
    )
    state["security_violations"] = sum(
        _stream_failure_class(entry) == "security" for entry in entries
    )
    state["evolution_update_failures"] = sum(
        _stream_failure_class(entry) == "evolution_update" for entry in entries
    )
    state["artifact_validation_failures"] = sum(
        _stream_failure_class(entry) == "artifact" for entry in entries
    )
    state["context_binding_violations"] = sum(
        _stream_failure_class(entry) == "context" for entry in entries
    )


def _stream_failure_class(entry: object) -> str | None:
    if type(entry) is not dict:
        return "infrastructure"
    status = entry.get("run_status")
    code = entry.get("failure_code")
    if status == TaskwiseRunStatusV1.SECURITY_TOOL_USE_VIOLATION.value:
        return "security"
    if status == TaskwiseRunStatusV1.TASKWISE_CONTEXT_BINDING_VIOLATION.value:
        return "context"
    if code in _ARTIFACT_FAILURE_CODES:
        return "artifact"
    if status == TaskwiseRunStatusV1.TASKWISE_EVOLUTION_UPDATE_FAILED.value:
        return "evolution_update"
    if status in {
        TaskwiseRunStatusV1.EXECUTION_FAILED.value,
        TaskwiseRunStatusV1.INITIALIZED.value,
        TaskwiseRunStatusV1.RUNNING.value,
    }:
        return "infrastructure"
    return None


def _completed_run_state(path: Path, *, expected_arm: str) -> dict[str, Any]:
    state = _read_public_run_state(path, expected_arm=expected_arm)
    if state.get("status") != "COMPLETED":
        raise TaskwiseCLIError("TASKWISE_RUN_NOT_COMPARABLE")
    return state


def _security_violation_count(state: dict[str, Any]) -> int:
    return int(state.get("status") == "SECURITY_TOOL_USE_VIOLATION")


def _strict_state_counter(state: dict[str, Any], field_name: str) -> int:
    value = state.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TaskwiseCLIError("TASKWISE_RUN_EVIDENCE_INVALID")
    return value


def _artifact_validation_failure_count(state: dict[str, Any]) -> int:
    failure = state.get("failure")
    return int(isinstance(failure, dict) and failure.get("code") == "ARTIFACT_VALIDATION_FAILED")


def _stream_public_evidence(
    output_directory: Path,
    *,
    expected_arm: str,
    state: dict[str, Any],
) -> tuple[tuple[int, ...], int]:
    """Read content-free memory/context evidence from the public event chain."""

    path = output_directory / "public" / "events.jsonl"
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TaskwiseCLIError("TASKWISE_PUBLIC_EVENT_EVIDENCE_INVALID") from exc
    if any(type(row) is not dict or row.get("arm") != expected_arm for row in rows):
        raise TaskwiseCLIError("TASKWISE_PUBLIC_EVENT_EVIDENCE_INVALID")

    findings = 0
    updates = [row for row in rows if row.get("kind") == "core_update"]
    completions = [row for row in rows if row.get("kind") == "completion"]
    if len(completions) != PILOT500_TASKS_PER_STREAM * 3:
        findings += 1
    if expected_arm == "control":
        if updates or any(row.get("memory") is not None for row in completions):
            findings += 1
        return (), findings
    if len(updates) != PILOT500_TASKS_PER_STREAM * 2:
        findings += 1

    memory_bytes: list[int] = []
    for index, row in enumerate(rows):
        if row.get("kind") != "core_update":
            continue
        try:
            metrics = TaskwiseMemoryPublicMetricsV1.from_dict(row.get("memory_metrics"))
        except (TypeError, ValueError):
            findings += 1
            continue
        memory_bytes.append(metrics.utf8_byte_count)
        output_memory = row.get("output_memory")
        if (
            type(output_memory) is not dict
            or row.get("core_job_state") != "COMPLETED"
            or row.get("context_resolution_digest")
            != output_memory.get("context_resolution_digest")
        ):
            findings += 1
        expected_round = row.get("update_index")
        if index + 1 >= len(rows):
            findings += 1
        else:
            next_row = rows[index + 1]
            if (
                next_row.get("kind") != "completion"
                or next_row.get("task_ordinal") != row.get("task_ordinal")
                or next_row.get("round_index") != expected_round
                or next_row.get("memory") != output_memory
            ):
                findings += 1

    round_zero = {
        row.get("task_ordinal"): row for row in completions if row.get("round_index") == 0
    }
    update_two = {row.get("task_ordinal"): row for row in updates if row.get("update_index") == 2}
    if round_zero.get(0, {}).get("memory") is not None:
        findings += 1
    for task_index in range(1, PILOT500_TASKS_PER_STREAM):
        if round_zero.get(task_index, {}).get("memory") != update_two.get(
            task_index - 1,
            {},
        ).get("output_memory"):
            findings += 1

    aggregate = state.get("memory_aggregate")
    if (
        type(aggregate) is not dict
        or aggregate.get("approved_artifact_count") != len(updates)
        or aggregate.get("max_utf8_byte_count") != max(memory_bytes, default=0)
    ):
        findings += 1
    return tuple(memory_bytes), findings


def _resolve_workspace_path(
    value: str,
    *,
    field_name: str,
    must_exist: bool,
) -> Path:
    if type(value) is not str or not value:
        raise TaskwiseCLIError(f"{field_name} is invalid")
    candidate = (WORKSPACE_ROOT / value).resolve()
    try:
        candidate.relative_to(WORKSPACE_ROOT.resolve())
    except ValueError as exc:
        raise TaskwiseCLIError(f"{field_name} escapes the workspace") from exc
    if must_exist and not candidate.exists():
        raise TaskwiseCLIError(f"{field_name} is unavailable")
    return candidate


def _git(*arguments: str) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=REPOSITORY_ROOT,
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise TaskwiseCLIError("TASKWISE_GIT_IDENTITY_UNAVAILABLE")
    return completed.stdout


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _safe_cli_payload(command: str, payload: dict[str, Any]) -> dict[str, object]:
    if command in {"compare", "compare-pilot-streams"}:
        encoded = _canonical_bytes(payload)
        canary_gate = payload.get("canary9_gate")
        canary_failed = type(canary_gate) is dict and canary_gate.get("passed") is not True
        report_incomplete = payload.get("status") in {"INCOMPLETE", "FAILED"}
        return {
            "status": "BLOCKED" if canary_failed or report_incomplete else "COMPLETED",
            "protocol_labels": list(PROTOCOL_LABELS),
            "private_report_sha256": hashlib.sha256(encoded).hexdigest(),
            "task_count": payload["task_count"],
            "stream_count": payload.get("stream_count"),
            "decision": payload.get("decision"),
            "canary9_gate": (
                None
                if type(canary_gate) is not dict
                else {
                    "labels": canary_gate.get("labels"),
                    "passed": canary_gate.get("passed"),
                    "finding_codes": canary_gate.get("finding_codes"),
                }
            ),
            "standard_chembench4k_score_claimed": False,
        }
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chembench4k-taskwise-online-v1")
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run-arm")
    run_parser.add_argument("--config", type=Path, required=True)
    run_parser.add_argument("--resume", action="store_true")
    suite_parser = commands.add_parser("run-pilot-stream-suite")
    suite_parser.add_argument("--arm", choices=("control", "online"), required=True)
    compare_parser = commands.add_parser("compare")
    compare_parser.add_argument("--scope", choices=_SCOPES, required=True)
    commands.add_parser("compare-pilot-streams")
    commands.add_parser("dry-run")
    arguments = parser.parse_args(argv)

    try:
        if arguments.command == "run-arm":
            payload = run_arm(arguments.config, resume=arguments.resume)
        elif arguments.command == "run-pilot-stream-suite":
            payload = run_pilot500_stream_suite(arm=arguments.arm)
        elif arguments.command == "compare":
            payload = compare(scope=arguments.scope)
        elif arguments.command == "compare-pilot-streams":
            payload = compare_pilot500_streams()
        else:
            payload = dry_run()
    except (TaskwiseCLIError, RuntimeError, ValueError, TypeError) as exc:
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "error_type": str(exc),
                    "model_calls": 0,
                },
                sort_keys=True,
            ),
            file=os.sys.stderr,
        )
        return 2
    safe_payload = _safe_cli_payload(arguments.command, payload)
    print(json.dumps(safe_payload, indent=2, sort_keys=True))
    if arguments.command in {"run-arm", "run-pilot-stream-suite"}:
        return 0 if safe_payload.get("status") == "COMPLETED" else 2
    return 2 if safe_payload.get("status") == "BLOCKED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
