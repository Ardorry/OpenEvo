"""Fail-closed CLI wiring for the paired taskwise online experiment."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from openevo_chembench.chembench4k_dataset import ChemBench4KDatasetLoader
from openevo_chembench.chembench4k_models import CHEMBENCH4K_CATEGORIES
from openevo_chembench.chembench4k_prompt import render_official_five_shot_prompt
from openevo_chembench.local_codex_executor import (
    LocalCodexCLIExecutor,
    LocalCodexExecutionError,
    _codex_command,
)
from openevo_chembench.meeting722_contract_v1 import (
    issue_meeting722_taskwise_contract_v1,
    verify_persisted_meeting722_taskwise_contract_v1,
)
from openevo_chembench.taskwise_config_v1 import (
    SOURCE_COMMIT_PLACEHOLDER,
    TaskwiseExperimentConfigV1,
    load_taskwise_config_v1,
    taskwise_arm_parity_findings,
)
from openevo_chembench.taskwise_attempt_v1 import (
    TaskwiseAttemptError,
    allocate_taskwise_control_attempt_v1,
    format_taskwise_attempt_id_v1,
    require_taskwise_completed_paired_attempt_v1,
    require_taskwise_active_arm_attempt_v1,
    require_taskwise_online_attempt_v1,
    taskwise_attempts_root_v1,
    validate_taskwise_attempt_id_v1,
    write_taskwise_attempt_suite_state_v1,
)
from openevo_chembench.taskwise_canary_receipt_v1 import (
    TaskwisePilotAuthorizationV1,
    current_taskwise_canary_generation_id_v1,
    default_taskwise_canary_receipt_inputs_v1,
    default_taskwise_canary_receipt_path_v1,
    verify_taskwise_paired_canary_receipt_v1,
    write_taskwise_paired_canary_receipt_v1,
)
from openevo_chembench.taskwise_core_evolution_v1 import (
    build_taskwise_core_port_at_roots_v1,
)
from openevo_chembench.taskwise_generation_v1 import (
    taskwise_canary_comparison_path_v1,
    taskwise_full_comparison_path_v1,
    taskwise_full_runtime_config_v1,
    taskwise_full_suite_state_path_v1,
    taskwise_pilot_comparison_path_v1,
    taskwise_pilot_runtime_config_v1,
    taskwise_pilot_suite_state_path_v1,
    validate_taskwise_generation_id_v1,
)
from openevo_chembench.taskwise_pilot_go_receipt_v1 import (
    TaskwiseFullAuthorizationV1,
    default_taskwise_pilot_go_receipt_inputs_v1,
    default_taskwise_pilot_go_receipt_path_v1,
    verify_taskwise_pilot_go_receipt_v1,
    write_taskwise_pilot_go_receipt_v1,
)
from openevo_chembench.taskwise_online_runner_v1 import (
    TaskwiseCoreUpdatePortV1,
    TaskwiseEpisodeV1,
    TaskwiseExecutionFailureV1,
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
    FULL_SIZE,
    FULL_STREAM_COUNT,
    FULL_STREAM_SCOPES,
    FULL_STREAM_TASK_COUNTS,
    PILOT500_STREAM_COUNT,
    PILOT500_STREAM_SCOPES,
    PILOT500_TASKS_PER_STREAM,
    TaskwiseManifestSet,
    select_taskwise_stream,
    verify_taskwise_manifests,
)
from openevo_chembench.taskwise_stream_statistics_v1 import (
    PrivateTaskwisePairedFullStreamV1,
    PrivateTaskwisePairedStreamV1,
    build_taskwise_full_stream_report_v1,
    build_taskwise_pilot500_stream_report_v1,
)
from openevo_chembench.source_identity_v2 import (
    SourceManifestError,
    verify_source_manifest,
)


_MODULE = Path(__file__).resolve()
PACKAGE_ROOT = _MODULE.parents[2]
REPOSITORY_ROOT = _MODULE.parents[4]
WORKSPACE_ROOT = REPOSITORY_ROOT
CONFIG_ROOT = PACKAGE_ROOT / "configs"
TASKWISE_STATE_ROOT = PACKAGE_ROOT / "state" / "taskwise_online_v1"
FRAMEWORK_LOCK = PACKAGE_ROOT / "state" / "v2" / "framework" / "framework-lock.json"
SOURCE_MANIFEST = PACKAGE_ROOT / "manifests" / "chembench_source_manifest_v2.json"
FULL4009_AUTHORIZATION = "GRANTED"
FULL4009_EXECUTION_ALLOWED = True
FULL4009_BLOCK_CODE = "USER_FULL_RUN_AUTHORIZATION_MISSING"
_SCOPES = (
    "canary9",
    "pilot500",
    *PILOT500_STREAM_SCOPES,
    *FULL_STREAM_SCOPES,
)
_PRIMARY_SCOPES = ("canary9", *PILOT500_STREAM_SCOPES, *FULL_STREAM_SCOPES)
_SUITE_RUN_STATUSES = frozenset({"COMPLETED", "INCOMPLETE", "FAILED"})
_TERMINAL_SECURITY_STATUSES = frozenset(
    {
        "MEETING_722_TASKWISE_CONTRACT_VIOLATION",
        "SECURITY_TOOL_USE_VIOLATION",
        "TASKWISE_EVOLUTION_UPDATE_FAILED",
        "TASKWISE_CONTEXT_BINDING_VIOLATION",
    }
)
_CONTRACT_FAILURE_CODES = frozenset(
    {
        "MEETING_722_TASKWISE_CONTRACT_VIOLATION",
    }
)
_ARTIFACT_FAILURE_CODES = frozenset(
    {
        "ARTIFACT_VALIDATION_FAILED",
        "TASKWISE_ARTIFACT_VALIDATION_FAILED",
        "TASKWISE_ARTIFACT_PROMOTION_FAILED",
        "TASKWISE_CORE_LINEAGE_INVALID",
        "TASKWISE_LINEAGE_FORK",
        "TASKWISE_LINEAGE_JUMP",
        "TASKWISE_LINEAGE_ROLLBACK",
        "TASKWISE_PREDECESSOR_BINDING_INVALID",
        "TASKWISE_RUNTIME_PREDECESSOR_FORK",
        "TASKWISE_TYPED_ARTIFACT_INVALID",
    }
)
_CONTEXT_FAILURE_CODES = frozenset(
    {
        "TASKWISE_CONTEXT_BINDING_VIOLATION",
        "TASKWISE_CONTEXT_IDENTITY_DRIFT",
        "TASKWISE_CONTEXT_RESOLUTION_FAILED",
        "TASKWISE_RUNTIME_MEMORY_REFERENCE_INVALID",
    }
)
_EVOLUTION_FAILURE_CODES = frozenset(
    {
        "TASKWISE_CORE_JOB_FAILED",
        "TASKWISE_CORE_UPDATE_FAILED",
        "TASKWISE_EVOLUTION_UPDATE_FAILED",
        "TASKWISE_JOB_IDENTITY_DRIFT",
    }
)
_SECURITY_FAILURE_CODES = frozenset(
    {
        "EXECUTOR_SECURITY_TOOL_USE_VIOLATION",
        "REFLECTOR_SECURITY_TOOL_USE_VIOLATION",
        "SECURITY_TOOL_USE_VIOLATION",
        "TASKWISE_REFLECTOR_SECURITY_TOOL_USE_VIOLATION",
        "TASKWISE_SECURITY_TOOL_USE_VIOLATION",
    }
)
_EXECUTOR_FAILURE_CODES = frozenset(
    {
        "EXECUTOR_PREFLIGHT_FAILED",
        "EXECUTOR_AUTH_MATERIALIZATION_FAILED",
        "EXECUTOR_PROCESS_SPAWN_FAILED",
        "EXECUTOR_CODEX_STARTUP_FAILED",
        "EXECUTOR_MODEL_TRANSPORT_FAILED",
        "EXECUTOR_TIMEOUT",
        "EXECUTOR_NONZERO_EXIT",
        "EXECUTOR_EVENT_STREAM_INVALID",
        "EXECUTOR_SECURITY_TOOL_USE_VIOLATION",
        "EXECUTOR_OUTPUT_MISSING",
        "EXECUTOR_OUTPUT_INVALID",
        "EXECUTOR_PRIVATE_DIAGNOSTICS_FAILED",
        "EXECUTOR_CLEANUP_FAILED",
        "EXECUTOR_POST_ATTESTATION_FAILED",
        "EXECUTOR_INTERNAL_ERROR",
        "EXECUTOR_PUBLIC_STATE_UNAVAILABLE",
        "EXECUTOR_SUITE_ORCHESTRATION_FAILED",
        "EXECUTOR_CONFIG_POLICY_AGENTS_ENABLED_FORBIDDEN",
        "EXECUTOR_CONFIG_POLICY_COMMAND_INVALID",
        "EXECUTOR_CONFIG_POLICY_CONFIG_MISMATCH",
        "EXECUTOR_CONFIG_POLICY_FEATURES_MISMATCH",
        "EXECUTOR_CONFIG_POLICY_PROBE_REJECTED",
        "EXECUTOR_CONFIG_POLICY_PROBE_UNAVAILABLE",
        "EXECUTOR_CONFIG_POLICY_VERSION_MISMATCH",
    }
)
_EXECUTOR_FAILURE_ALIASES = {
    "UNCLASSIFIED_EXECUTOR_FAILURE": "EXECUTOR_INTERNAL_ERROR",
    "FAIL_CLOSED_INTERNAL_ERROR": "EXECUTOR_INTERNAL_ERROR",
    "INVALID_EXECUTOR_RESULT": "EXECUTOR_OUTPUT_INVALID",
    "INFRASTRUCTURE_TRANSPORT_FAILURE": "EXECUTOR_MODEL_TRANSPORT_FAILED",
    "RUN_STATE_UNAVAILABLE": "EXECUTOR_PUBLIC_STATE_UNAVAILABLE",
    "SUITE_ORCHESTRATION_ERROR": "EXECUTOR_SUITE_ORCHESTRATION_FAILED",
}
_TASKWISE_CODEX_REQUIRED_FLAGS = frozenset(
    {
        "--ephemeral",
        "--ignore-rules",
        "--ignore-user-config",
        "--json",
        "--skip-git-repo-check",
        "--strict-config",
    }
)
_TASKWISE_CODEX_FORBIDDEN_FLAGS = frozenset(
    {
        "--add-dir",
        "--dangerously-bypass-approvals-and-sandbox",
        "--enable",
        "--search",
    }
)
_TASKWISE_CODEX_REQUIRED_DISABLED_FEATURES = frozenset(
    {
        "apps",
        "auth_elicitation",
        "browser_use",
        "browser_use_external",
        "browser_use_full_cdp_access",
        "code_mode",
        "code_mode_host",
        "code_mode_only",
        "computer_use",
        "enable_fanout",
        "enable_mcp_apps",
        "goals",
        "hooks",
        "image_generation",
        "in_app_browser",
        "memories",
        "multi_agent",
        "multi_agent_v2",
        "plugin_sharing",
        "plugins",
        "request_permissions_tool",
        "remote_plugin",
        "shell_snapshot",
        "shell_tool",
        "skill_mcp_dependency_install",
        "tool_call_mcp_elicitation",
        "tool_suggest",
        "unified_exec",
        "workspace_dependencies",
    }
)
_TASKWISE_CODEX_EXPECTED_CONFIG = {
    "approval_policy": '"never"',
    "allow_login_shell": "false",
    "check_for_update_on_startup": "false",
    "forced_login_method": '"chatgpt"',
    "model_reasoning_effort": '"medium"',
    "shell_environment_policy.inherit": '"none"',
    "web_search": '"disabled"',
}
_CODEX_POLICY_PROBE_TIMEOUT_SECONDS = 15.0
_CLI_PUBLIC_FAILURE_CODES = (
    _EXECUTOR_FAILURE_CODES
    | _ARTIFACT_FAILURE_CODES
    | _CONTEXT_FAILURE_CODES
    | _EVOLUTION_FAILURE_CODES
    | _SECURITY_FAILURE_CODES
    | frozenset(
        {
            "REFLECTOR_FILESYSTEM_ISOLATION_MISSING",
            "TASKWISE_ARM_PARITY_MISMATCH",
            "TASKWISE_ATTEMPT_ALREADY_COMPARED",
            "TASKWISE_ATTEMPT_ALREADY_EXISTS",
            "TASKWISE_ATTEMPT_AUTHORITY_INVALID",
            "TASKWISE_ATTEMPT_EVIDENCE_INVALID",
            "TASKWISE_ATTEMPT_ID_INVALID",
            "TASKWISE_ATTEMPT_INCOMPLETE",
            "TASKWISE_ATTEMPT_NAMESPACE_INVALID",
            "TASKWISE_ATTEMPT_NOT_ACTIVE",
            "TASKWISE_ATTEMPT_RETRY_FORBIDDEN",
            "TASKWISE_ATTEMPT_SEQUENCE_INVALID",
            "TASKWISE_ATTEMPT_SUITE_STATE_INVALID",
            "TASKWISE_ATTEMPTS_ROOT_INVALID",
            "TASKWISE_CLI_INTERNAL_ERROR",
            "TASKWISE_COMPARISON_OUTPUT_EXISTS",
            "TASKWISE_COMPARISON_STORAGE_INVALID",
            "TASKWISE_CONFIG_PATH_BINDING_MISMATCH",
            "TASKWISE_CONTROL_MEMORY_EVIDENCE_INVALID",
            "TASKWISE_CORE_PREFLIGHT_FAILED",
            "TASKWISE_CORE_RUN_STATE_MISMATCH",
            "TASKWISE_EPISODE_COUNT_MISMATCH",
            "TASKWISE_GIT_IDENTITY_UNAVAILABLE",
            "TASKWISE_OUTPUT_TARGET_EXISTS",
            "TASKWISE_PACKAGE_SOURCE_DIRTY",
            "TASKWISE_PAIRED_CONFIG_UNAVAILABLE",
            "TASKWISE_PAIRED_ATTEMPT_CONTROL_INCOMPLETE",
            "TASKWISE_PAIRED_ATTEMPT_CONTROL_REQUIRED",
            "TASKWISE_PAIRED_ATTEMPT_INCOMPLETE",
            "TASKWISE_PAIRED_ATTEMPT_MISMATCH",
            "TASKWISE_PAIRED_ATTEMPT_MISSING",
            "TASKWISE_PAIRED_ATTEMPT_ONLINE_EXISTS",
            "TASKWISE_PAIRED_CANARY_RECEIPT_EXISTS",
            "TASKWISE_PAIRED_CANARY_RECEIPT_INVALID",
            "TASKWISE_PILOT_GO_RECEIPT_EXISTS",
            "TASKWISE_PILOT_GO_RECEIPT_INVALID",
            "TASKWISE_FULL_RESUME_FORBIDDEN",
            "TASKWISE_GENERATION_ID_INVALID",
            "TASKWISE_PILOT_RESUME_FORBIDDEN",
            "TASKWISE_PAIRED_RUN_BINDING_MISMATCH",
            "TASKWISE_PUBLIC_EVENT_EVIDENCE_INVALID",
            "TASKWISE_RESUME_OUTPUT_MISSING",
            "TASKWISE_RUN_EVIDENCE_INVALID",
            "TASKWISE_RUNTIME_ID_INVALID",
            "TASKWISE_RUN_NOT_COMPARABLE",
            "TASKWISE_RUN_STATE_INVALID",
            "TASKWISE_RUN_STATE_UNAVAILABLE",
            "TASKWISE_SCOPE_INVALID",
            "TASKWISE_SOURCE_COMMIT_MISMATCH",
            "TASKWISE_SOURCE_MANIFEST_MISMATCH",
            "TASKWISE_STREAM_TASK_COUNT_MISMATCH",
            "TASKWISE_SUITE_ARM_INVALID",
            "TASKWISE_SUITE_EVIDENCE_INVALID",
            "TASKWISE_SUITE_RESULT_INVALID",
            "TASKWISE_SUITE_STATE_INVALID",
            "TASKWISE_SUITE_STATE_EXISTS",
            "TASKWISE_SUITE_TERMINAL_FAILURE",
            "TASKWISE_VERIFIED_FRAMEWORK_LOCK_MISSING",
            "USER_FULL_RUN_AUTHORIZATION_MISSING",
        }
    )
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
CodexPolicyProbeRunnerV1 = Callable[
    [Sequence[str], Path, Mapping[str, str], float],
    tuple[int, str],
]
CodexPolicyGateV1 = Callable[[TaskwiseExperimentConfigV1], dict[str, object]]


class _ClosedExecutorFailureAdapterV1:
    """Preserve sanitized Local Codex failure semantics at the runner boundary."""

    def __init__(self, delegate: TaskwiseExecutorV1) -> None:
        self._delegate = delegate

    def execute_taskwise(self, request):
        try:
            return self._delegate.execute_taskwise(request)
        except LocalCodexExecutionError as exc:
            code = exc.taskwise_failure_code
            if code not in _EXECUTOR_FAILURE_CODES:
                code = "EXECUTOR_INTERNAL_ERROR"
            converted = TaskwiseExecutionFailureV1(
                code,
                completion_observed=exc.completion_observed,
                diagnostic_receipt=exc.diagnostic_receipt,
                event_digest=exc.event_digest,
                event_counts=exc.event_counts,
                executor_stage=exc.executor_stage,
            )
            raise converted from None

    def consume_taskwise_context_receipt(self, session_id: str):
        return self._delegate.consume_taskwise_context_receipt(session_id)

    def close(self) -> None:
        close = getattr(self._delegate, "close", None)
        if callable(close):
            close()


def inspect_taskwise_codex_policy_v1(
    config: TaskwiseExperimentConfigV1,
    *,
    command: tuple[str, ...] | None = None,
    probe_runner: CodexPolicyProbeRunnerV1 | None = None,
) -> dict[str, object]:
    """Validate the exact Codex policy with a non-model config parser probe.

    The returned receipt is deliberately closed: neither stderr nor arbitrary
    subprocess text can enter public state.
    """

    if type(config) is not TaskwiseExperimentConfigV1:
        raise TypeError("config must be exact TaskwiseExperimentConfigV1")
    executable = shutil.which("codex")
    actual_command = command
    if actual_command is None and executable is not None:
        actual_command = _codex_command(
            executable=Path(executable),
            model=config.model,
            reasoning_effort=config.reasoning_effort,
            workspace=Path("/__openevo_taskwise_empty_workdir__"),
            output_last_message=Path("/__openevo_taskwise_output_last_message__"),
        )

    findings: set[str] = set()
    config_entries: dict[str, str] = {}
    disabled_features: frozenset[str] = frozenset()
    if actual_command is None:
        findings.add("EXECUTOR_CONFIG_POLICY_PROBE_UNAVAILABLE")
        normalized_command: tuple[str, ...] = ()
    else:
        normalized_command = _normalized_policy_command(actual_command)
        try:
            config_entries = _command_key_value_entries(actual_command, "--config")
            disabled_features = frozenset(_command_values(actual_command, "--disable"))
            _validate_static_codex_policy(
                config=config,
                command=actual_command,
                config_entries=config_entries,
                disabled_features=disabled_features,
            )
        except TaskwiseCLIError as exc:
            code = str(exc)
            findings.add(
                code
                if code in _EXECUTOR_FAILURE_CODES
                else "EXECUTOR_CONFIG_POLICY_COMMAND_INVALID"
            )

    version_output = ""
    probe_returncode: int | None = None
    if actual_command is not None:
        runner = probe_runner or _default_codex_policy_probe_runner_v1
        try:
            with tempfile.TemporaryDirectory(
                prefix=".taskwise-codex-policy-preflight-"
            ) as temporary:
                root = Path(temporary)
                work = root / "work"
                home = root / "home"
                codex_home = root / "codex_home"
                xdg_config = root / "xdg_config"
                xdg_cache = root / "xdg_cache"
                xdg_state = root / "xdg_state"
                tmp = root / "tmp"
                for path in (
                    work,
                    home,
                    codex_home,
                    xdg_config,
                    xdg_cache,
                    xdg_state,
                    tmp,
                ):
                    path.mkdir(mode=0o700)
                environment = {
                    "CODEX_HOME": os.fspath(codex_home),
                    "HOME": os.fspath(home),
                    "LANG": "C.UTF-8",
                    "PATH": os.environ.get("PATH", os.defpath),
                    "TMP": os.fspath(tmp),
                    "TMPDIR": os.fspath(tmp),
                    "XDG_CACHE_HOME": os.fspath(xdg_cache),
                    "XDG_CONFIG_HOME": os.fspath(xdg_config),
                    "XDG_STATE_HOME": os.fspath(xdg_state),
                }
                version_returncode, version_output = runner(
                    (actual_command[0], "--version"),
                    work,
                    environment,
                    _CODEX_POLICY_PROBE_TIMEOUT_SECONDS,
                )
                probe_returncode, _probe_output = runner(
                    _no_model_codex_policy_probe_command(actual_command),
                    work,
                    environment,
                    _CODEX_POLICY_PROBE_TIMEOUT_SECONDS,
                )
        except (OSError, RuntimeError, TypeError, ValueError):
            findings.add("EXECUTOR_CONFIG_POLICY_PROBE_UNAVAILABLE")
        else:
            expected_version = f"codex-cli {config.codex_cli_version}"
            if version_returncode != 0 or version_output.strip() != expected_version:
                findings.add("EXECUTOR_CONFIG_POLICY_VERSION_MISMATCH")
            if probe_returncode != 0:
                findings.add("EXECUTOR_CONFIG_POLICY_PROBE_REJECTED")

    receipt = {
        "schema_version": "taskwise_codex_policy_preflight_v1",
        "status": "PASS" if not findings else "BLOCKED",
        "finding_codes": sorted(findings),
        "codex_cli_version": config.codex_cli_version,
        "config_keys": sorted(config_entries),
        "disabled_features": sorted(disabled_features),
        "policy_sha256": hashlib.sha256(
            _canonical_bytes(
                {
                    "codex_cli_version": config.codex_cli_version,
                    "command": list(normalized_command),
                }
            )
        ).hexdigest(),
        "model_calls": 0,
        "stderr_included": False,
    }
    if set(receipt) != {
        "schema_version",
        "status",
        "finding_codes",
        "codex_cli_version",
        "config_keys",
        "disabled_features",
        "policy_sha256",
        "model_calls",
        "stderr_included",
    }:
        raise AssertionError("Codex policy receipt schema drifted")
    return receipt


def verify_taskwise_codex_policy_v1(
    config: TaskwiseExperimentConfigV1,
    *,
    command: tuple[str, ...] | None = None,
    probe_runner: CodexPolicyProbeRunnerV1 | None = None,
) -> dict[str, object]:
    """Require the closed no-model policy receipt before paid construction."""

    receipt = inspect_taskwise_codex_policy_v1(
        config,
        command=command,
        probe_runner=probe_runner,
    )
    findings = receipt["finding_codes"]
    if type(findings) is not list or any(code not in _EXECUTOR_FAILURE_CODES for code in findings):
        raise TaskwiseCLIError("EXECUTOR_CONFIG_POLICY_COMMAND_INVALID")
    if findings:
        raise TaskwiseCLIError(findings[0])
    return receipt


def _authorized_pilot_generation_v1(
    authorization: TaskwisePilotAuthorizationV1,
    requested_generation_id: str | None,
) -> str:
    if type(authorization) is not TaskwisePilotAuthorizationV1:
        raise TaskwiseCLIError("TASKWISE_PAIRED_CANARY_RECEIPT_INVALID")
    candidate = (
        authorization.paired_canary_generation_id
        if requested_generation_id is None
        else requested_generation_id
    )
    try:
        generation = validate_taskwise_generation_id_v1(candidate)
    except (TypeError, ValueError) as exc:
        raise TaskwiseCLIError("TASKWISE_GENERATION_ID_INVALID") from exc
    if generation != authorization.paired_canary_generation_id:
        raise TaskwiseCLIError("TASKWISE_GENERATION_ID_INVALID")
    return generation


def _authorized_full_generation_v1(
    authorization: TaskwiseFullAuthorizationV1,
    requested_generation_id: str | None,
) -> str:
    if type(authorization) is not TaskwiseFullAuthorizationV1:
        raise TaskwiseCLIError("TASKWISE_PILOT_GO_RECEIPT_INVALID")
    candidate = (
        authorization.full_generation_id
        if requested_generation_id is None
        else requested_generation_id
    )
    try:
        generation = validate_taskwise_generation_id_v1(candidate)
    except (TypeError, ValueError) as exc:
        raise TaskwiseCLIError("TASKWISE_GENERATION_ID_INVALID") from exc
    if generation != authorization.full_generation_id:
        raise TaskwiseCLIError("TASKWISE_GENERATION_ID_INVALID")
    return generation


def run_arm(
    config_path: Path,
    *,
    resume: bool = False,
    generation_id: str | None = None,
    attempt_id: str | None = None,
    executor_factory: ExecutorFactoryV1 | None = None,
    core_port_factory: CorePortFactoryV1 | None = None,
    source_gate: SourceGateV1 | None = None,
    executor_policy_gate: CodexPolicyGateV1 | None = None,
) -> dict[str, object]:
    """Validate every frozen input before constructing paid runtime objects."""

    template_config = load_taskwise_config_v1(config_path.resolve())
    authorization: TaskwisePilotAuthorizationV1 | TaskwiseFullAuthorizationV1 | None = None
    if template_config.scope in PILOT500_STREAM_SCOPES:
        if resume:
            raise TaskwiseCLIError("TASKWISE_PILOT_RESUME_FORBIDDEN")
        authorization = require_taskwise_pilot_authorization_v1()
        generation_id = _authorized_pilot_generation_v1(
            authorization,
            generation_id,
        )
        try:
            attempt_id = validate_taskwise_attempt_id_v1(
                attempt_id or format_taskwise_attempt_id_v1(1)
            )
        except ValueError as exc:
            raise TaskwiseCLIError("TASKWISE_ATTEMPT_ID_INVALID") from exc
    elif template_config.scope in FULL_STREAM_SCOPES:
        _require_user_full_run_authorization_v1()
        if resume:
            raise TaskwiseCLIError("TASKWISE_FULL_RESUME_FORBIDDEN")
        authorization = require_taskwise_full_authorization_v1()
        generation_id = _authorized_full_generation_v1(
            authorization,
            generation_id,
        )
        try:
            attempt_id = validate_taskwise_attempt_id_v1(
                attempt_id or format_taskwise_attempt_id_v1(1)
            )
        except ValueError as exc:
            raise TaskwiseCLIError("TASKWISE_ATTEMPT_ID_INVALID") from exc
    elif generation_id is not None or attempt_id is not None:
        raise TaskwiseCLIError("TASKWISE_GENERATION_ID_INVALID")
    gate = source_gate or verify_taskwise_source_gate_v1
    current_commit = gate(template_config)
    loader, manifest, paired_templates = _verify_static_inputs(
        template_config,
        config_path,
    )
    if generation_id is None:
        config = template_config
        paired_configs = paired_templates
    elif template_config.scope in PILOT500_STREAM_SCOPES:
        assert attempt_id is not None
        config = taskwise_pilot_runtime_config_v1(
            template_config,
            generation_id,
            attempt_id,
        )
        paired_configs = {
            arm: taskwise_pilot_runtime_config_v1(
                paired_templates[arm],
                generation_id,
                attempt_id,
            )
            for arm in ("control", "online")
        }
    else:
        assert attempt_id is not None
        config = taskwise_full_runtime_config_v1(
            template_config,
            generation_id,
            attempt_id,
        )
        paired_configs = {
            arm: taskwise_full_runtime_config_v1(
                paired_templates[arm],
                generation_id,
                attempt_id,
            )
            for arm in ("control", "online")
        }
    if template_config.scope in PILOT500_STREAM_SCOPES:
        assert generation_id is not None and attempt_id is not None
        try:
            require_taskwise_active_arm_attempt_v1(
                REPOSITORY_ROOT,
                suite_kind="pilot500",
                generation_id=generation_id,
                attempt_id=attempt_id,
                arm=config.arm,
                scope=template_config.scope,
            )
        except (OSError, TaskwiseAttemptError, ValueError) as exc:
            raise TaskwiseCLIError(str(exc)) from exc
    elif template_config.scope in FULL_STREAM_SCOPES:
        assert generation_id is not None and attempt_id is not None
        try:
            require_taskwise_active_arm_attempt_v1(
                REPOSITORY_ROOT,
                suite_kind="full",
                generation_id=generation_id,
                attempt_id=attempt_id,
                arm=config.arm,
                scope=template_config.scope,
            )
        except (OSError, TaskwiseAttemptError, ValueError) as exc:
            raise TaskwiseCLIError(str(exc)) from exc
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
    if executor_policy_gate is not None:
        executor_policy_gate(config)
    elif executor_factory is None:
        verify_taskwise_codex_policy_v1(config)
    selected_executor_factory = executor_factory or build_default_taskwise_executor_v1
    selected_core_factory = core_port_factory or build_default_taskwise_core_port_v1
    executor = _ClosedExecutorFailureAdapterV1(selected_executor_factory(config))
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
    generation_id: str | None = None,
    attempt_id: str | None = None,
    arm_runner: Callable[..., dict[str, object]] | None = None,
    suite_state_path: Path | None = None,
    source_gate: SourceGateV1 | None = None,
) -> dict[str, object]:
    """Start every stream once inside one fresh immutable arm generation."""

    if arm not in {"control", "online"}:
        raise TaskwiseCLIError("TASKWISE_SUITE_ARM_INVALID")
    authorization = require_taskwise_pilot_authorization_v1()
    generation = _authorized_pilot_generation_v1(authorization, generation_id)
    gate = source_gate or verify_taskwise_source_gate_v1
    preflight: list[tuple[str, Path, TaskwiseExperimentConfigV1]] = []
    for scope in PILOT500_STREAM_SCOPES:
        config_path = CONFIG_ROOT / f"{arm}_{scope}_taskwise_online_v1.yaml"
        template_config = load_taskwise_config_v1(config_path)
        gate(template_config)
        _verify_static_inputs(template_config, config_path)
        preflight.append((scope, config_path, template_config))
    try:
        if suite_state_path is not None:
            attempt = validate_taskwise_attempt_id_v1(
                attempt_id or format_taskwise_attempt_id_v1(1)
            )
        elif arm == "control":
            attempt = allocate_taskwise_control_attempt_v1(
                REPOSITORY_ROOT,
                suite_kind="pilot500",
                generation_id=generation,
                requested_attempt_id=attempt_id,
            )
        else:
            attempt = require_taskwise_online_attempt_v1(
                REPOSITORY_ROOT,
                suite_kind="pilot500",
                generation_id=generation,
                requested_attempt_id=attempt_id,
            )
    except (OSError, TaskwiseAttemptError, ValueError) as exc:
        raise TaskwiseCLIError(str(exc)) from exc
    runner = arm_runner or run_arm
    state_path = suite_state_path or taskwise_pilot_suite_state_path_v1(
        REPOSITORY_ROOT,
        generation_id=generation,
        arm=arm,
        attempt_id=attempt,
    )
    state = _load_or_initialize_suite_state(state_path, arm=arm)
    state["generation_id"] = generation
    state["attempt_id"] = attempt
    state["paired_canary_generation_id"] = authorization.paired_canary_generation_id
    state["canary_receipt_sha256"] = authorization.receipt_sha256
    state["pilot_binding_sha256"] = authorization.pilot_binding_sha256
    state_persisted = False

    def persist_state() -> None:
        nonlocal state_persisted
        if suite_state_path is None:
            try:
                write_taskwise_attempt_suite_state_v1(
                    REPOSITORY_ROOT,
                    suite_kind="pilot500",
                    generation_id=generation,
                    attempt_id=attempt,
                    arm=arm,
                    state=state,
                    create_only=not state_persisted,
                )
            except TaskwiseAttemptError as exc:
                raise TaskwiseCLIError(str(exc)) from exc
        else:
            _write_suite_state(
                state_path,
                state,
                create_only=not state_persisted,
            )
        state_persisted = True

    for scope, config_path, template_config in preflight:
        state["status"] = "INCOMPLETE"
        state["active_scope"] = scope
        state["streams"][scope] = {
            "action": "STARTING",
            "run_status": "MISSING",
            "resume_allowed": False,
            "failure_code": None,
            "completed": False,
            "missing": True,
        }
        _update_suite_totals(state)
        persist_state()
        try:
            config = taskwise_pilot_runtime_config_v1(
                template_config,
                generation,
                attempt,
            )
            output = _resolve_workspace_path(
                config.output_directory,
                field_name="stream output_directory",
                must_exist=False,
            )
            action, evidence = _suite_stream_action(output, expected_arm=arm)
            if action == "terminal":
                state["streams"][scope] = {
                    "action": "TERMINAL_FAILURE",
                    **evidence,
                }
                state["active_scope"] = None
                state["status"] = "FAILED"
                _update_suite_totals(state)
                persist_state()
                raise TaskwiseCLIError("TASKWISE_SUITE_TERMINAL_FAILURE")

            runner_arguments = {
                "resume": False,
                "generation_id": generation,
            }
            if arm_runner is None:
                runner_arguments["attempt_id"] = attempt
            result = runner(config_path, **runner_arguments)
            result_evidence = _suite_result_evidence(result)
        except Exception as exc:
            if state.get("status") == "FAILED":
                raise
            failure_evidence = _orchestration_failure_evidence(exc)
            state["streams"][scope] = {
                "action": "ORCHESTRATION_ERROR",
                **failure_evidence,
            }
            state["active_scope"] = None
            state["status"] = "FAILED"
            _update_suite_totals(state)
            persist_state()
            raise
        state["streams"][scope] = {
            "action": "STARTED",
            **result_evidence,
        }
        if result_evidence["completed"]:
            state["streams"][scope]["action"] = "COMPLETED"
            state["active_scope"] = None
            _update_suite_totals(state)
            persist_state()
            continue
        # The paid suite never resumes or stitches streams.  Any synchronous
        # non-completion closes this attempt; only the next paired attempt may
        # restart from stream zero, and only for closed infrastructure codes.
        state["status"] = "FAILED"
        state["active_scope"] = None
        _update_suite_totals(state)
        persist_state()
        raise TaskwiseCLIError("TASKWISE_SUITE_TERMINAL_FAILURE")

    state["status"] = "COMPLETED"
    state["active_scope"] = None
    _update_suite_totals(state)
    persist_state()
    return state


def run_full_stream_suite(
    *,
    arm: str,
    generation_id: str | None = None,
    attempt_id: str | None = None,
    arm_runner: Callable[..., dict[str, object]] | None = None,
    suite_state_path: Path | None = None,
    source_gate: SourceGateV1 | None = None,
) -> dict[str, object]:
    """Start all full streams once; existing evidence forbids resume/stitch."""

    if arm not in {"control", "online"}:
        raise TaskwiseCLIError("TASKWISE_SUITE_ARM_INVALID")
    _require_user_full_run_authorization_v1()
    authorization = require_taskwise_full_authorization_v1()
    generation = _authorized_full_generation_v1(authorization, generation_id)
    gate = source_gate or verify_taskwise_source_gate_v1
    preflight: list[tuple[str, Path, TaskwiseExperimentConfigV1]] = []
    for scope in FULL_STREAM_SCOPES:
        config_path = CONFIG_ROOT / f"{arm}_{scope}_taskwise_online_v1.yaml"
        template_config = load_taskwise_config_v1(config_path)
        gate(template_config)
        _verify_static_inputs(template_config, config_path)
        preflight.append((scope, config_path, template_config))
    try:
        if suite_state_path is not None:
            attempt = validate_taskwise_attempt_id_v1(
                attempt_id or format_taskwise_attempt_id_v1(1)
            )
        elif arm == "control":
            attempt = allocate_taskwise_control_attempt_v1(
                REPOSITORY_ROOT,
                suite_kind="full",
                generation_id=generation,
                requested_attempt_id=attempt_id,
            )
        else:
            attempt = require_taskwise_online_attempt_v1(
                REPOSITORY_ROOT,
                suite_kind="full",
                generation_id=generation,
                requested_attempt_id=attempt_id,
            )
    except (OSError, TaskwiseAttemptError, ValueError) as exc:
        raise TaskwiseCLIError(str(exc)) from exc
    runner = arm_runner or run_arm
    state_path = suite_state_path or taskwise_full_suite_state_path_v1(
        REPOSITORY_ROOT,
        generation_id=generation,
        arm=arm,
        attempt_id=attempt,
    )
    counts = dict(zip(FULL_STREAM_SCOPES, FULL_STREAM_TASK_COUNTS, strict=True))
    state = _load_or_initialize_suite_state(
        state_path,
        arm=arm,
        stream_scopes=FULL_STREAM_SCOPES,
        stream_task_counts=counts,
        schema_version="taskwise_full_stream_suite_state_v1",
    )
    state["generation_id"] = generation
    state["attempt_id"] = attempt
    state["pilot_generation_id"] = authorization.pilot_generation_id
    state["pilot_attempt_id"] = authorization.pilot_attempt_id
    state["pilot_go_receipt_sha256"] = authorization.receipt_sha256
    state["pilot_report_sha256"] = authorization.pilot_report_sha256
    state["full_binding_sha256"] = authorization.full_binding_sha256
    state_persisted = False

    def persist_state() -> None:
        nonlocal state_persisted
        if suite_state_path is None:
            try:
                write_taskwise_attempt_suite_state_v1(
                    REPOSITORY_ROOT,
                    suite_kind="full",
                    generation_id=generation,
                    attempt_id=attempt,
                    arm=arm,
                    state=state,
                    create_only=not state_persisted,
                )
            except TaskwiseAttemptError as exc:
                raise TaskwiseCLIError(str(exc)) from exc
        else:
            _write_suite_state(
                state_path,
                state,
                create_only=not state_persisted,
            )
        state_persisted = True

    for scope, config_path, template_config in preflight:
        state["status"] = "INCOMPLETE"
        state["active_scope"] = scope
        state["streams"][scope] = {
            "action": "STARTING",
            "run_status": "MISSING",
            "resume_allowed": False,
            "failure_code": None,
            "completed": False,
            "missing": True,
        }
        _update_suite_totals(state)
        persist_state()
        try:
            config = taskwise_full_runtime_config_v1(
                template_config,
                generation,
                attempt,
            )
            output = _resolve_workspace_path(
                config.output_directory,
                field_name="full stream output_directory",
                must_exist=False,
            )
            action, evidence = _suite_stream_action(output, expected_arm=arm)
            if action == "terminal":
                state["streams"][scope] = {
                    "action": "TERMINAL_FAILURE",
                    **evidence,
                }
                state["active_scope"] = None
                state["status"] = "FAILED"
                _update_suite_totals(state)
                persist_state()
                raise TaskwiseCLIError("TASKWISE_SUITE_TERMINAL_FAILURE")

            runner_arguments = {
                "resume": False,
                "generation_id": generation,
            }
            if arm_runner is None:
                runner_arguments["attempt_id"] = attempt
            result = runner(config_path, **runner_arguments)
            result_evidence = _suite_result_evidence(result)
        except Exception as exc:
            if state.get("status") == "FAILED":
                raise
            failure_evidence = _orchestration_failure_evidence(exc)
            state["streams"][scope] = {
                "action": "ORCHESTRATION_ERROR",
                **failure_evidence,
            }
            state["active_scope"] = None
            state["status"] = "FAILED"
            _update_suite_totals(state)
            persist_state()
            raise
        state["streams"][scope] = {
            "action": "STARTED",
            **result_evidence,
        }
        if result_evidence["completed"]:
            state["streams"][scope]["action"] = "COMPLETED"
            state["active_scope"] = None
            _update_suite_totals(state)
            persist_state()
            continue
        state["status"] = "FAILED"
        state["active_scope"] = None
        _update_suite_totals(state)
        persist_state()
        raise TaskwiseCLIError("TASKWISE_SUITE_TERMINAL_FAILURE")

    state["status"] = "COMPLETED"
    state["active_scope"] = None
    _update_suite_totals(state)
    persist_state()
    return state


def dry_run(
    *,
    executor_policy_gate: CodexPolicyGateV1 | None = None,
) -> dict[str, object]:
    """Recompute configs, parity, dataset, and manifests without paid objects."""

    scopes: dict[str, object] = {}
    codex_policy_preflight: dict[str, object] | None = None
    for scope in _PRIMARY_SCOPES:
        control_path = CONFIG_ROOT / f"control_{scope}_taskwise_online_v1.yaml"
        control = load_taskwise_config_v1(control_path)
        if codex_policy_preflight is None:
            gate = executor_policy_gate or verify_taskwise_codex_policy_v1
            codex_policy_preflight = gate(control)
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
        "codex_policy_preflight": codex_policy_preflight,
        "pilot500_stream_design": {
            "stream_count": PILOT500_STREAM_COUNT,
            "tasks_per_stream": PILOT500_TASKS_PER_STREAM,
            "total_item_count": (PILOT500_STREAM_COUNT * PILOT500_TASKS_PER_STREAM),
            "generation_zero_reset_between_streams": True,
            "legacy_single_chain_scope": "pilot500",
        },
        "full_stream_design": {
            "stream_count": FULL_STREAM_COUNT,
            "per_stream_task_count": list(FULL_STREAM_TASK_COUNTS),
            "total_item_count": FULL_SIZE,
            "generation_zero_reset_between_streams": True,
            "pilot_go_receipt_required": True,
        },
        "scopes": scopes,
    }


def compare(
    *,
    scope: str,
    generation_id: str | None = None,
    attempt_id: str | None = None,
    persist: bool = True,
    source_gate: SourceGateV1 | None = None,
) -> dict[str, Any]:
    """Build the private non-standard report from two completed paired arms."""

    if scope not in _SCOPES:
        raise TaskwiseCLIError("TASKWISE_SCOPE_INVALID")
    if scope in PILOT500_STREAM_SCOPES and persist:
        # Per-stream reports are internal inputs to the attempt-scoped aggregate.
        # The legacy scope root is not an authorized persistence namespace.
        raise TaskwiseCLIError("TASKWISE_COMPARISON_STORAGE_INVALID")
    control_path = CONFIG_ROOT / f"control_{scope}_taskwise_online_v1.yaml"
    control_template = load_taskwise_config_v1(control_path)
    if scope == "canary9":
        if attempt_id is not None:
            raise TaskwiseCLIError("TASKWISE_ATTEMPT_ID_INVALID")
        current_generation = current_taskwise_canary_generation_id_v1(
            default_taskwise_canary_receipt_inputs_v1(PACKAGE_ROOT)
        )
        if generation_id is not None and generation_id != current_generation:
            raise TaskwiseCLIError("TASKWISE_GENERATION_ID_INVALID")
        generation_id = current_generation
    elif scope in PILOT500_STREAM_SCOPES:
        authorization = require_taskwise_pilot_authorization_v1()
        generation_id = _authorized_pilot_generation_v1(
            authorization,
            generation_id,
        )
        try:
            attempt_id = validate_taskwise_attempt_id_v1(
                attempt_id or format_taskwise_attempt_id_v1(1)
            )
        except ValueError as exc:
            raise TaskwiseCLIError("TASKWISE_ATTEMPT_ID_INVALID") from exc
    elif scope in FULL_STREAM_SCOPES:
        authorization = require_taskwise_full_authorization_v1()
        generation_id = _authorized_full_generation_v1(
            authorization,
            generation_id,
        )
        try:
            attempt_id = validate_taskwise_attempt_id_v1(
                attempt_id or format_taskwise_attempt_id_v1(1)
            )
        except ValueError as exc:
            raise TaskwiseCLIError("TASKWISE_ATTEMPT_ID_INVALID") from exc
    elif generation_id is not None or attempt_id is not None:
        raise TaskwiseCLIError("TASKWISE_GENERATION_ID_INVALID")
    gate = source_gate or verify_taskwise_source_gate_v1
    current_commit = gate(control_template)
    loader, manifest, paired_templates = _verify_static_inputs(
        control_template,
        control_path,
    )
    if scope in PILOT500_STREAM_SCOPES:
        assert generation_id is not None and attempt_id is not None
        paired = {
            arm: taskwise_pilot_runtime_config_v1(
                paired_templates[arm],
                generation_id,
                attempt_id,
            )
            for arm in ("control", "online")
        }
    elif scope in FULL_STREAM_SCOPES:
        assert generation_id is not None and attempt_id is not None
        paired = {
            arm: taskwise_full_runtime_config_v1(
                paired_templates[arm],
                generation_id,
                attempt_id,
            )
            for arm in ("control", "online")
        }
    else:
        paired = paired_templates
    control = paired["control"]
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
    _verify_meeting722_run_contract(
        output_directory=control_output,
        config=control,
        state=control_state,
        episodes=episodes,
    )
    _verify_meeting722_run_contract(
        output_directory=online_output,
        config=online,
        state=online_state,
        episodes=episodes,
    )

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
        if scope == "canary9":
            assert generation_id is not None
            path = taskwise_canary_comparison_path_v1(
                REPOSITORY_ROOT,
                generation_id,
            )
        elif scope in PILOT500_STREAM_SCOPES:
            path = (
                _resolve_workspace_path(
                    f"OpenEvo/results/chembench4k_taskwise_online_v1/{scope}",
                    field_name="comparison output root",
                    must_exist=True,
                )
                / "private"
                / "taskwise_comparison_v1.json"
            )
        else:
            # Full per-stream comparisons are internal inputs to the single
            # aggregate report.  Persisting them would create independently
            # mutable evidence beside the authoritative full comparison.
            raise TaskwiseCLIError("TASKWISE_COMPARISON_STORAGE_INVALID")
        _persist_private_comparison_report_v1(path, report)
    return report


def compare_pilot500_streams(
    *,
    generation_id: str | None = None,
    attempt_id: str | None = None,
    persist: bool = True,
    source_gate: SourceGateV1 | None = None,
) -> dict[str, Any]:
    """Aggregate ten separately executed paired streams without joining memory."""

    authorization = require_taskwise_pilot_authorization_v1()
    generation = _authorized_pilot_generation_v1(authorization, generation_id)
    try:
        attempts_root = taskwise_attempts_root_v1(
            REPOSITORY_ROOT,
            suite_kind="pilot500",
            generation_id=generation,
        )
        if not attempts_root.exists() and not persist:
            attempt = validate_taskwise_attempt_id_v1(
                attempt_id or format_taskwise_attempt_id_v1(1)
            )
        elif persist:
            attempt = require_taskwise_completed_paired_attempt_v1(
                REPOSITORY_ROOT,
                suite_kind="pilot500",
                generation_id=generation,
                requested_attempt_id=attempt_id,
            )
        else:
            candidates = tuple(
                sorted(
                    (
                        child.name
                        for child in attempts_root.iterdir()
                        if child.is_dir() and not child.is_symlink()
                    )
                )
            )
            attempt = validate_taskwise_attempt_id_v1(
                attempt_id or (candidates[-1] if candidates else format_taskwise_attempt_id_v1(1))
            )
    except (OSError, TaskwiseAttemptError, ValueError) as exc:
        raise TaskwiseCLIError(str(exc)) from exc
    run_evidence = _pilot_stream_run_evidence(
        source_gate=source_gate,
        generation_id=generation,
        attempt_id=attempt,
    )
    if any(not item["completed"] for item in run_evidence):
        report = _incomplete_pilot_stream_report(run_evidence)
        if persist:
            _persist_pilot_stream_report(
                report,
                generation_id=generation,
                attempt_id=attempt,
            )
        return report

    streams: list[PrivateTaskwisePairedStreamV1] = []
    for scope in PILOT500_STREAM_SCOPES:
        paired_report = compare(
            scope=scope,
            generation_id=generation,
            attempt_id=attempt,
            persist=False,
            source_gate=source_gate,
        )
        if paired_report["task_count"] != PILOT500_TASKS_PER_STREAM:
            raise TaskwiseCLIError("TASKWISE_STREAM_TASK_COUNT_MISMATCH")
        control_path = CONFIG_ROOT / f"control_{scope}_taskwise_online_v1.yaml"
        control_template = load_taskwise_config_v1(control_path)
        gate = source_gate or verify_taskwise_source_gate_v1
        gate(control_template)
        _loader, _manifest, paired_templates = _verify_static_inputs(
            control_template,
            control_path,
        )
        paired = {
            arm: taskwise_pilot_runtime_config_v1(
                paired_templates[arm],
                generation,
                attempt,
            )
            for arm in ("control", "online")
        }
        control = paired["control"]
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
    report["meeting722_contract_violations"] = 0
    if type(report.get("go_gate")) is dict:
        report["go_gate"]["meeting722_contract_violations_required"] = 0
    if persist:
        _persist_pilot_stream_report(
            report,
            generation_id=generation,
            attempt_id=attempt,
        )
    return report


def compare_full_streams(
    *,
    generation_id: str | None = None,
    attempt_id: str | None = None,
    persist: bool = True,
    source_gate: SourceGateV1 | None = None,
) -> dict[str, Any]:
    """Aggregate all 4,009 tasks without joining independent memory streams."""

    authorization = require_taskwise_full_authorization_v1()
    generation = _authorized_full_generation_v1(authorization, generation_id)
    try:
        attempts_root = taskwise_attempts_root_v1(
            REPOSITORY_ROOT,
            suite_kind="full",
            generation_id=generation,
        )
        if not attempts_root.exists() and not persist:
            attempt = validate_taskwise_attempt_id_v1(
                attempt_id or format_taskwise_attempt_id_v1(1)
            )
        elif persist:
            attempt = require_taskwise_completed_paired_attempt_v1(
                REPOSITORY_ROOT,
                suite_kind="full",
                generation_id=generation,
                requested_attempt_id=attempt_id,
            )
        else:
            candidates = tuple(
                sorted(
                    (
                        child.name
                        for child in attempts_root.iterdir()
                        if child.is_dir() and not child.is_symlink()
                    )
                )
            )
            attempt = validate_taskwise_attempt_id_v1(
                attempt_id or (candidates[-1] if candidates else format_taskwise_attempt_id_v1(1))
            )
    except (OSError, TaskwiseAttemptError, ValueError) as exc:
        raise TaskwiseCLIError(str(exc)) from exc
    run_evidence = _full_stream_run_evidence(
        source_gate=source_gate,
        generation_id=generation,
        attempt_id=attempt,
    )
    if any(not item["completed"] for item in run_evidence):
        report = _incomplete_full_stream_report(run_evidence)
        if persist:
            _persist_full_stream_report(
                report,
                generation_id=generation,
                attempt_id=attempt,
            )
        return report

    streams: list[PrivateTaskwisePairedFullStreamV1] = []
    for scope, expected_count in zip(
        FULL_STREAM_SCOPES,
        FULL_STREAM_TASK_COUNTS,
        strict=True,
    ):
        paired_report = compare(
            scope=scope,
            generation_id=generation,
            attempt_id=attempt,
            persist=False,
            source_gate=source_gate,
        )
        if paired_report["task_count"] != expected_count:
            raise TaskwiseCLIError("TASKWISE_STREAM_TASK_COUNT_MISMATCH")
        control_path = CONFIG_ROOT / f"control_{scope}_taskwise_online_v1.yaml"
        control_template = load_taskwise_config_v1(control_path)
        gate = source_gate or verify_taskwise_source_gate_v1
        gate(control_template)
        _loader, _manifest, paired_templates = _verify_static_inputs(
            control_template,
            control_path,
        )
        paired = {
            arm: taskwise_full_runtime_config_v1(
                paired_templates[arm],
                generation,
                attempt,
            )
            for arm in ("control", "online")
        }
        outputs = {
            arm: _resolve_workspace_path(
                paired[arm].output_directory,
                field_name=f"{arm} full output_directory",
                must_exist=True,
            )
            for arm in ("control", "online")
        }
        states = {
            arm: _completed_run_state(outputs[arm], expected_arm=arm)
            for arm in ("control", "online")
        }
        control_memory_metrics, control_context_findings = _stream_public_evidence(
            outputs["control"],
            expected_arm="control",
            state=states["control"],
            expected_task_count=expected_count,
        )
        online_memory_metrics, online_context_findings = _stream_public_evidence(
            outputs["online"],
            expected_arm="online",
            state=states["online"],
            expected_task_count=expected_count,
        )
        if control_memory_metrics:
            raise TaskwiseCLIError("TASKWISE_CONTROL_MEMORY_EVIDENCE_INVALID")
        streams.append(
            PrivateTaskwisePairedFullStreamV1(
                stream_id=scope,
                control=load_private_taskwise_results_v1(outputs["control"]),
                online=load_private_taskwise_results_v1(outputs["online"]),
                control_security_violations=_security_violation_count(states["control"]),
                online_security_violations=_security_violation_count(states["online"]),
                online_artifact_validation_failures=(
                    _artifact_validation_failure_count(states["online"])
                ),
                context_binding_violations=(control_context_findings + online_context_findings),
                completed=(
                    states["control"]["status"] == "COMPLETED"
                    and states["online"]["status"] == "COMPLETED"
                ),
                approved_memory_utf8_bytes=online_memory_metrics,
            )
        )
    report = build_taskwise_full_stream_report_v1(tuple(streams))
    report["meeting722_contract_violations"] = 0
    if persist:
        _persist_full_stream_report(
            report,
            generation_id=generation,
            attempt_id=attempt,
        )
    return report


def _full_stream_run_evidence(
    *,
    source_gate: SourceGateV1 | None,
    generation_id: str,
    attempt_id: str,
) -> tuple[dict[str, object], ...]:
    """Collect public-only full run evidence before private result access."""

    gate = source_gate or verify_taskwise_source_gate_v1
    evidence: list[dict[str, object]] = []
    for scope in FULL_STREAM_SCOPES:
        for arm in ("control", "online"):
            config_path = CONFIG_ROOT / f"{arm}_{scope}_taskwise_online_v1.yaml"
            template_config = load_taskwise_config_v1(config_path)
            gate(template_config)
            _verify_static_inputs(template_config, config_path)
            config = taskwise_full_runtime_config_v1(
                template_config,
                generation_id,
                attempt_id,
            )
            output = _resolve_workspace_path(
                config.output_directory,
                field_name=f"{arm} full output_directory",
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
                    "failure_code": "EXECUTOR_PUBLIC_STATE_UNAVAILABLE",
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


def _incomplete_full_stream_report(
    evidence: tuple[dict[str, object], ...],
) -> dict[str, Any]:
    if len(evidence) != FULL_STREAM_COUNT * 2:
        raise TaskwiseCLIError("TASKWISE_SUITE_EVIDENCE_INVALID")
    grouped: dict[str, dict[str, dict[str, object]]] = {scope: {} for scope in FULL_STREAM_SCOPES}
    for item in evidence:
        stream_id = item.get("stream_id")
        arm = item.get("arm")
        if stream_id not in grouped or arm not in {"control", "online"}:
            raise TaskwiseCLIError("TASKWISE_SUITE_EVIDENCE_INVALID")
        grouped[stream_id][arm] = item
    if any(set(pair) != {"control", "online"} for pair in grouped.values()):
        raise TaskwiseCLIError("TASKWISE_SUITE_EVIDENCE_INVALID")

    counters = {
        "infrastructure_failures": 0,
        "security_violations": 0,
        "evolution_update_failures": 0,
        "artifact_validation_failures": 0,
        "context_binding_violations": 0,
        "meeting722_contract_violations": 0,
    }
    terminal = False
    per_stream: list[dict[str, object]] = []
    for scope in FULL_STREAM_SCOPES:
        pair = grouped[scope]
        pair_completed = all(pair[arm]["completed"] is True for arm in ("control", "online"))
        row: dict[str, object] = {
            "stream_id": scope,
            "completed": pair_completed,
        }
        for arm in ("control", "online"):
            item = pair[arm]
            failure_class = _stream_failure_class(item)
            if item.get("missing") is True or failure_class == "infrastructure":
                counters["infrastructure_failures"] += 1
            elif failure_class == "security":
                counters["security_violations"] += 1
                terminal = True
            elif failure_class == "evolution_update":
                counters["evolution_update_failures"] += 1
                terminal = True
            elif failure_class == "artifact":
                counters["artifact_validation_failures"] += 1
                terminal = True
            elif failure_class == "context":
                terminal = True
            elif failure_class == "contract":
                counters["meeting722_contract_violations"] += 1
                terminal = True
            context_count = item.get("context_binding_violations")
            if isinstance(context_count, bool) or not isinstance(context_count, int):
                raise TaskwiseCLIError("TASKWISE_SUITE_EVIDENCE_INVALID")
            counters["context_binding_violations"] += context_count
            terminal = terminal or context_count > 0
            row[f"{arm}_status"] = item.get("run_status")
            row[f"{arm}_failure_code"] = item.get("failure_code")
        per_stream.append(row)
    return {
        "schema_version": "taskwise_online_full_ten_stream_report_v1",
        "protocol_id": "taskwise_online_evolution_v1",
        "protocol_labels": list(PROTOCOL_LABELS),
        "standard_chembench4k_score_claimed": False,
        "status": "FAILED" if terminal else "INCOMPLETE",
        "stream_count": FULL_STREAM_COUNT,
        "per_stream_task_count": dict(
            zip(FULL_STREAM_SCOPES, FULL_STREAM_TASK_COUNTS, strict=True)
        ),
        "task_count": FULL_SIZE,
        "completed_streams": sum(row["completed"] is True for row in per_stream),
        "per_stream": per_stream,
        "private_results_read": False,
        **counters,
    }


def _pilot_stream_run_evidence(
    *,
    source_gate: SourceGateV1 | None,
    generation_id: str,
    attempt_id: str,
) -> tuple[dict[str, object], ...]:
    """Collect only public run-state evidence before any private result read."""

    gate = source_gate or verify_taskwise_source_gate_v1
    evidence: list[dict[str, object]] = []
    for scope in PILOT500_STREAM_SCOPES:
        for arm in ("control", "online"):
            config_path = CONFIG_ROOT / f"{arm}_{scope}_taskwise_online_v1.yaml"
            template_config = load_taskwise_config_v1(config_path)
            gate(template_config)
            _verify_static_inputs(template_config, config_path)
            config = taskwise_pilot_runtime_config_v1(
                template_config,
                generation_id,
                attempt_id,
            )
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
                    "failure_code": "EXECUTOR_PUBLIC_STATE_UNAVAILABLE",
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
    meeting722_contract_violations = 0
    executor_failures = 0
    executor_failure_codes: dict[str, int] = {}
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
            executor_code = _executor_failure_code(item.get("failure_code"))
            if executor_code is not None:
                executor_failures += 1
                executor_failure_codes[executor_code] = (
                    executor_failure_codes.get(executor_code, 0) + 1
                )
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
            elif failure_class == "contract":
                meeting722_contract_violations += 1
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
            stream_payload[f"{arm}_executor_failure_code"] = executor_code
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
        "meeting722_contract_violations": meeting722_contract_violations,
        "executor_failures": executor_failures,
        "executor_failure_codes": dict(sorted(executor_failure_codes.items())),
        "go_gate": {
            "completed_streams_required": PILOT500_STREAM_COUNT,
            "security_violations_required": 0,
            "artifact_validation_failures_required": 0,
            "context_binding_violations_required": 0,
            "meeting722_contract_violations_required": 0,
            "mean_stream_delta_minimum": 0.02,
            "stream_block_bootstrap_ci_lower_must_exceed": 0.0,
            "positive_stream_count_minimum": 7,
        },
    }


def _persist_pilot_stream_report(
    report: dict[str, Any],
    *,
    generation_id: str,
    attempt_id: str,
) -> None:
    path = taskwise_pilot_comparison_path_v1(
        REPOSITORY_ROOT,
        generation_id,
        attempt_id,
    )
    _persist_private_comparison_report_v1(path, report)


def _persist_full_stream_report(
    report: dict[str, Any],
    *,
    generation_id: str,
    attempt_id: str,
) -> None:
    path = taskwise_full_comparison_path_v1(
        REPOSITORY_ROOT,
        generation_id,
        attempt_id,
    )
    _persist_private_comparison_report_v1(path, report)


def _persist_private_comparison_report_v1(
    path: Path,
    report: dict[str, Any],
) -> None:
    """Persist one immutable private report beneath an attested generation."""

    parent_descriptor: int | None = None
    report_descriptor: int | None = None
    try:
        payload = _canonical_bytes(report)
        parent_descriptor = _open_private_comparison_parent_v1(path)
        flags = (
            os.O_CREAT
            | os.O_EXCL
            | os.O_WRONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            report_descriptor = os.open(
                path.name,
                flags,
                0o600,
                dir_fd=parent_descriptor,
            )
        except FileExistsError as exc:
            existing_descriptor: int | None = None
            try:
                existing_descriptor = os.open(
                    path.name,
                    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent_descriptor,
                )
                existing_metadata = os.fstat(existing_descriptor)
                with os.fdopen(existing_descriptor, "rb") as existing_stream:
                    existing_descriptor = None
                    existing_payload = existing_stream.read()
            except OSError as read_error:
                raise TaskwiseCLIError("TASKWISE_COMPARISON_STORAGE_INVALID") from read_error
            finally:
                if existing_descriptor is not None:
                    os.close(existing_descriptor)
            if (
                not stat.S_ISREG(existing_metadata.st_mode)
                or stat.S_IMODE(existing_metadata.st_mode) != 0o600
                or existing_metadata.st_uid != os.geteuid()
                or existing_payload != payload
            ):
                raise TaskwiseCLIError("TASKWISE_COMPARISON_OUTPUT_EXISTS") from exc
            return
        os.fchmod(report_descriptor, 0o600)
        metadata = os.fstat(report_descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.geteuid()
        ):
            raise OSError("private comparison report attestation failed")
        with os.fdopen(report_descriptor, "wb") as stream:
            report_descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(parent_descriptor)
    except TaskwiseCLIError:
        raise
    except OSError as exc:
        raise TaskwiseCLIError("TASKWISE_COMPARISON_STORAGE_INVALID") from exc
    finally:
        if report_descriptor is not None:
            try:
                os.close(report_descriptor)
            except OSError:
                pass
        if parent_descriptor is not None:
            try:
                os.close(parent_descriptor)
            except OSError:
                pass


def _open_private_comparison_parent_v1(path: Path) -> int:
    """Create and open the private generation directory without symlinks."""

    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        raise OSError("private comparison path is invalid")
    try:
        relative = path.relative_to(REPOSITORY_ROOT)
    except ValueError as exc:
        raise OSError("private comparison path escapes repository") from exc
    parts = relative.parts
    valid_canary = (
        len(parts) == 7
        and parts[:4]
        == (
            "results",
            "chembench4k_taskwise_online_v1",
            "canary9",
            "generations",
        )
        and parts[5:] == ("private", "taskwise_comparison_v1.json")
    )
    valid_pilot = (
        len(parts) == 8
        and parts[:3]
        == (
            "results",
            "chembench4k_taskwise_online_v1",
            "pilot500_generations",
        )
        and parts[4] == "attempts"
        and parts[6:] == ("private", "taskwise_stream_comparison_v1.json")
    )
    valid_full = (
        len(parts) == 8
        and parts[:3]
        == (
            "results",
            "chembench4k_taskwise_online_v1",
            "full_generations",
        )
        and parts[4] == "attempts"
        and parts[6:] == ("private", "taskwise_full_stream_comparison_v1.json")
    )
    if not (valid_canary or valid_pilot or valid_full):
        raise OSError("private comparison path schema is invalid")
    try:
        if valid_canary:
            validate_taskwise_generation_id_v1(parts[-3])
        else:
            validate_taskwise_generation_id_v1(parts[3])
            validate_taskwise_attempt_id_v1(parts[5])
    except (TypeError, ValueError) as exc:
        raise OSError("private comparison generation is invalid") from exc

    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    current_descriptor: int | None = None
    try:
        current_descriptor = os.open(REPOSITORY_ROOT, directory_flags)
        root_metadata = os.fstat(current_descriptor)
        if not stat.S_ISDIR(root_metadata.st_mode) or root_metadata.st_uid != os.geteuid():
            raise OSError("repository root attestation failed")
        for index, component in enumerate(parts[:-1]):
            created = False
            try:
                child_descriptor = os.open(
                    component,
                    directory_flags,
                    dir_fd=current_descriptor,
                )
            except FileNotFoundError:
                os.mkdir(component, mode=0o700, dir_fd=current_descriptor)
                child_descriptor = os.open(
                    component,
                    directory_flags,
                    dir_fd=current_descriptor,
                )
                os.fchmod(child_descriptor, 0o700)
                created = True
            previous_descriptor = current_descriptor
            current_descriptor = child_descriptor
            os.close(previous_descriptor)
            metadata = os.fstat(current_descriptor)
            private_threshold = 4 if valid_canary else 3
            is_generation_private = index >= private_threshold
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or ((created or is_generation_private) and stat.S_IMODE(metadata.st_mode) != 0o700)
            ):
                raise OSError("private comparison directory attestation failed")
        assert current_descriptor is not None
        result = current_descriptor
        current_descriptor = None
        return result
    finally:
        if current_descriptor is not None:
            try:
                os.close(current_descriptor)
            except OSError:
                pass


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


def _current_canary_generation_v1(requested: str | None = None) -> str:
    inputs = default_taskwise_canary_receipt_inputs_v1(PACKAGE_ROOT)
    try:
        current = current_taskwise_canary_generation_id_v1(inputs)
        if requested is not None:
            validate_taskwise_generation_id_v1(requested)
            if requested != current:
                raise ValueError
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise TaskwiseCLIError("TASKWISE_GENERATION_ID_INVALID") from exc
    return current


def freeze_taskwise_paired_canary_receipt_v1(
    generation_id: str | None = None,
) -> dict[str, object]:
    """Freeze the fixed paired-canary receipt after recomputing every gate."""

    inputs = default_taskwise_canary_receipt_inputs_v1(PACKAGE_ROOT)
    generation = _current_canary_generation_v1(generation_id)
    receipt_path = default_taskwise_canary_receipt_path_v1(
        PACKAGE_ROOT,
        generation,
    )
    if receipt_path.exists() or receipt_path.is_symlink():
        try:
            authorization = verify_taskwise_paired_canary_receipt_v1(
                inputs,
                receipt_path,
            )
            receipt_sha256 = authorization.receipt_sha256
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise TaskwiseCLIError("TASKWISE_PAIRED_CANARY_RECEIPT_EXISTS") from exc
        return {
            "schema_version": "taskwise_paired_canary_receipt_freeze_v1",
            "status": "PASS",
            "paid_pilot_allowed": True,
            "receipt_sha256": receipt_sha256,
            "evidence_digest": authorization.evidence_digest,
            "receipt_path": receipt_path.relative_to(REPOSITORY_ROOT).as_posix(),
            "paired_canary_generation_id": generation,
            "model_calls": 0,
        }
    try:
        receipt = write_taskwise_paired_canary_receipt_v1(inputs, receipt_path)
        receipt_sha256 = hashlib.sha256(receipt.canonical_bytes()).hexdigest()
    except FileExistsError:
        return freeze_taskwise_paired_canary_receipt_v1(generation)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise TaskwiseCLIError("TASKWISE_PAIRED_CANARY_RECEIPT_INVALID") from exc
    return {
        "schema_version": "taskwise_paired_canary_receipt_freeze_v1",
        "status": "PASS",
        "paid_pilot_allowed": receipt.paid_pilot_allowed,
        "receipt_sha256": receipt_sha256,
        "evidence_digest": receipt.evidence_digest,
        "receipt_path": receipt_path.relative_to(REPOSITORY_ROOT).as_posix(),
        "paired_canary_generation_id": generation,
        "model_calls": 0,
    }


def require_taskwise_pilot_authorization_v1(
    paired_canary_generation_id: str | None = None,
) -> TaskwisePilotAuthorizationV1:
    """Recompute the fixed current receipt and issue the pilot capability."""

    generation = _current_canary_generation_v1(paired_canary_generation_id)
    try:
        return verify_taskwise_paired_canary_receipt_v1(
            default_taskwise_canary_receipt_inputs_v1(PACKAGE_ROOT),
            default_taskwise_canary_receipt_path_v1(PACKAGE_ROOT, generation),
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise TaskwiseCLIError("TASKWISE_PAIRED_CANARY_RECEIPT_INVALID") from exc


def verify_taskwise_pilot_receipt_v1(
    paired_canary_generation_id: str | None = None,
) -> dict[str, object]:
    """Expose only content-free authorization digests to the CLI."""

    authorization = require_taskwise_pilot_authorization_v1(paired_canary_generation_id)
    return {
        "schema_version": "taskwise_pilot_authorization_verification_v1",
        "status": "PASS",
        "receipt_sha256": authorization.receipt_sha256,
        "evidence_digest": authorization.evidence_digest,
        "source_commit": authorization.source_commit,
        "pilot_binding_sha256": authorization.pilot_binding_sha256,
        "paired_canary_generation_id": authorization.paired_canary_generation_id,
        "model_calls": 0,
    }


def freeze_taskwise_pilot_go_receipt_v1(
    pilot_generation_id: str | None = None,
    pilot_attempt_id: str | None = None,
) -> dict[str, object]:
    """Freeze one immutable full-run authority after recomputing pilot GO."""

    pilot_authorization = require_taskwise_pilot_authorization_v1()
    generation = _authorized_pilot_generation_v1(
        pilot_authorization,
        pilot_generation_id,
    )
    try:
        completed_attempt = require_taskwise_completed_paired_attempt_v1(
            REPOSITORY_ROOT,
            suite_kind="pilot500",
            generation_id=generation,
            requested_attempt_id=pilot_attempt_id,
        )
    except (OSError, TaskwiseAttemptError, ValueError) as exc:
        raise TaskwiseCLIError(str(exc)) from exc
    inputs = default_taskwise_pilot_go_receipt_inputs_v1(PACKAGE_ROOT)
    receipt_path = default_taskwise_pilot_go_receipt_path_v1(
        PACKAGE_ROOT,
        generation,
    )
    if receipt_path.exists() or receipt_path.is_symlink():
        try:
            authorization = verify_taskwise_pilot_go_receipt_v1(
                inputs,
                receipt_path,
            )
            receipt_sha256 = authorization.receipt_sha256
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise TaskwiseCLIError("TASKWISE_PILOT_GO_RECEIPT_EXISTS") from exc
        if authorization.pilot_attempt_id != completed_attempt:
            raise TaskwiseCLIError("TASKWISE_PAIRED_ATTEMPT_MISMATCH")
        return {
            "schema_version": "taskwise_pilot_go_receipt_freeze_v1",
            "status": "PASS",
            "paid_full_allowed": True,
            "receipt_sha256": receipt_sha256,
            "evidence_digest": authorization.evidence_digest,
            "pilot_generation_id": generation,
            "pilot_attempt_id": completed_attempt,
            "full_generation_id": authorization.full_generation_id,
            "receipt_path": receipt_path.relative_to(REPOSITORY_ROOT).as_posix(),
            "model_calls": 0,
        }
    try:
        receipt = write_taskwise_pilot_go_receipt_v1(inputs, receipt_path)
        receipt_sha256 = hashlib.sha256(receipt.canonical_bytes()).hexdigest()
    except FileExistsError:
        return freeze_taskwise_pilot_go_receipt_v1(
            generation,
            completed_attempt,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise TaskwiseCLIError("TASKWISE_PILOT_GO_RECEIPT_INVALID") from exc
    return {
        "schema_version": "taskwise_pilot_go_receipt_freeze_v1",
        "status": "PASS",
        "paid_full_allowed": receipt.paid_full_allowed,
        "receipt_sha256": receipt_sha256,
        "evidence_digest": receipt.evidence_digest,
        "pilot_generation_id": generation,
        "pilot_attempt_id": completed_attempt,
        "full_generation_id": receipt.fields["full_generation_id"],
        "receipt_path": receipt_path.relative_to(REPOSITORY_ROOT).as_posix(),
        "model_calls": 0,
    }


def require_taskwise_full_authorization_v1(
    pilot_generation_id: str | None = None,
    pilot_attempt_id: str | None = None,
) -> TaskwiseFullAuthorizationV1:
    """Recompute pilot GO and issue the exact full generation capability."""

    pilot_authorization = require_taskwise_pilot_authorization_v1()
    generation = _authorized_pilot_generation_v1(
        pilot_authorization,
        pilot_generation_id,
    )
    try:
        authorization = verify_taskwise_pilot_go_receipt_v1(
            default_taskwise_pilot_go_receipt_inputs_v1(PACKAGE_ROOT),
            default_taskwise_pilot_go_receipt_path_v1(PACKAGE_ROOT, generation),
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise TaskwiseCLIError("TASKWISE_PILOT_GO_RECEIPT_INVALID") from exc
    if pilot_attempt_id is not None:
        try:
            requested = validate_taskwise_attempt_id_v1(pilot_attempt_id)
        except ValueError as exc:
            raise TaskwiseCLIError("TASKWISE_ATTEMPT_ID_INVALID") from exc
        if requested != authorization.pilot_attempt_id:
            raise TaskwiseCLIError("TASKWISE_PAIRED_ATTEMPT_MISMATCH")
    return authorization


def verify_taskwise_full_receipt_v1(
    pilot_generation_id: str | None = None,
    pilot_attempt_id: str | None = None,
) -> dict[str, object]:
    """Expose only content-free, recomputed full authorization evidence."""

    authorization = require_taskwise_full_authorization_v1(
        pilot_generation_id,
        pilot_attempt_id,
    )
    return {
        "schema_version": "taskwise_full_authorization_verification_v1",
        "status": "PASS",
        "receipt_sha256": authorization.receipt_sha256,
        "evidence_digest": authorization.evidence_digest,
        "source_commit": authorization.source_commit,
        "canary_receipt_sha256": authorization.canary_receipt_sha256,
        "pilot_generation_id": authorization.pilot_generation_id,
        "pilot_attempt_id": authorization.pilot_attempt_id,
        "pilot_report_sha256": authorization.pilot_report_sha256,
        "full_binding_sha256": authorization.full_binding_sha256,
        "full_generation_id": authorization.full_generation_id,
        "model_calls": 0,
    }


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
            / config.run_name
            / config.arm
        ),
    )


def build_default_taskwise_core_port_v1(
    config: TaskwiseExperimentConfigV1,
) -> TaskwiseCoreUpdatePortV1:
    """Construct the shared registered-method Core port at the formal run root."""

    if type(config) is not TaskwiseExperimentConfigV1 or config.arm != "online":
        raise TypeError("default Core port requires the online taskwise config")
    state_root = TASKWISE_STATE_ROOT / config.scope / config.run_name
    output_root = _resolve_workspace_path(
        config.output_directory,
        field_name="output_directory",
        must_exist=False,
    )
    if output_root.exists() != state_root.exists():
        raise TaskwiseCLIError("TASKWISE_CORE_RUN_STATE_MISMATCH")
    try:
        return build_taskwise_core_port_at_roots_v1(
            state_root=state_root,
            framework_lock=FRAMEWORK_LOCK,
            timeout_seconds=config.executor.timeout_seconds,
            memory_limits=config.memory_limits,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise TaskwiseCLIError("TASKWISE_CORE_PREFLIGHT_FAILED") from exc


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
    try:
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
            stream_id=config.scope,
            memory_limits=config.memory_limits,
        )
    except ValueError as exc:
        if str(exc) == "run_id must be a bounded runtime identifier":
            raise TaskwiseCLIError("TASKWISE_RUNTIME_ID_INVALID") from exc
        raise


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


def _command_values(command: Sequence[str], flag: str) -> tuple[str, ...]:
    if (
        isinstance(command, (str, bytes))
        or not isinstance(command, Sequence)
        or any(type(item) is not str for item in command)
        or type(flag) is not str
    ):
        raise TaskwiseCLIError("EXECUTOR_CONFIG_POLICY_COMMAND_INVALID")
    values: list[str] = []
    index = 0
    while index < len(command):
        if command[index] != flag:
            index += 1
            continue
        if index + 1 >= len(command) or command[index + 1].startswith("--"):
            raise TaskwiseCLIError("EXECUTOR_CONFIG_POLICY_COMMAND_INVALID")
        values.append(command[index + 1])
        index += 2
    return tuple(values)


def _command_key_value_entries(
    command: Sequence[str],
    flag: str,
) -> dict[str, str]:
    entries: dict[str, str] = {}
    for value in _command_values(command, flag):
        key, separator, raw_value = value.partition("=")
        if separator != "=" or not key or not raw_value or key.strip() != key or key in entries:
            raise TaskwiseCLIError("EXECUTOR_CONFIG_POLICY_COMMAND_INVALID")
        entries[key] = raw_value
    return entries


def _validate_static_codex_policy(
    *,
    config: TaskwiseExperimentConfigV1,
    command: Sequence[str],
    config_entries: Mapping[str, str],
    disabled_features: frozenset[str],
) -> None:
    cd_values = _command_values(command, "--cd")
    output_values = _command_values(command, "--output-last-message")
    if (
        len(command) < 2
        or command[1] != "exec"
        or _command_values(command, "--model") != (config.model,)
        or _command_values(command, "--sandbox") != ("read-only",)
        or len(cd_values) != 1
        or len(output_values) != 1
        or command[-1] != "-"
        or not _TASKWISE_CODEX_REQUIRED_FLAGS.issubset(command)
        or _TASKWISE_CODEX_FORBIDDEN_FLAGS.intersection(command)
    ):
        raise TaskwiseCLIError("EXECUTOR_CONFIG_POLICY_COMMAND_INVALID")
    if "agents.enabled" in config_entries:
        raise TaskwiseCLIError("EXECUTOR_CONFIG_POLICY_AGENTS_ENABLED_FORBIDDEN")
    expected_config = dict(_TASKWISE_CODEX_EXPECTED_CONFIG)
    expected_config["model_reasoning_effort"] = f'"{config.reasoning_effort}"'
    if dict(config_entries) != expected_config:
        raise TaskwiseCLIError("EXECUTOR_CONFIG_POLICY_CONFIG_MISMATCH")
    if disabled_features != _TASKWISE_CODEX_REQUIRED_DISABLED_FEATURES:
        raise TaskwiseCLIError("EXECUTOR_CONFIG_POLICY_FEATURES_MISMATCH")
    expected_command = _codex_command(
        executable=Path(command[0]),
        model=config.model,
        reasoning_effort=config.reasoning_effort,
        workspace=Path(cd_values[0]),
        output_last_message=Path(output_values[0]),
    )
    if tuple(command) != expected_command:
        raise TaskwiseCLIError("EXECUTOR_CONFIG_POLICY_COMMAND_INVALID")


def _normalized_policy_command(command: Sequence[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    replace_next = False
    for index, item in enumerate(command):
        if index == 0:
            normalized.append("<CODEX_EXECUTABLE>")
        elif replace_next:
            normalized.append("<EMPTY_WORKDIR>")
            replace_next = False
        else:
            normalized.append(item)
            replace_next = item == "--cd"
    return tuple(normalized)


def _no_model_codex_policy_probe_command(
    command: Sequence[str],
) -> tuple[str, ...]:
    if not command:
        raise TaskwiseCLIError("EXECUTOR_CONFIG_POLICY_COMMAND_INVALID")
    probe: list[str] = [
        command[0],
        "debug",
        "prompt-input",
    ]
    for flag in ("--disable", "--config"):
        for value in _command_values(command, flag):
            probe.extend((flag, value))
    probe.append("TASKWISE_CONFIG_POLICY_PREFLIGHT_NO_MODEL")
    if "exec" in probe or "--model" in probe:
        raise AssertionError("non-model Codex policy probe became executable")
    return tuple(probe)


def _default_codex_policy_probe_runner_v1(
    command: Sequence[str],
    cwd: Path,
    environment: Mapping[str, str],
    timeout_seconds: float,
) -> tuple[int, str]:
    """Run only version/config-parser commands and discard stderr completely."""

    try:
        result = subprocess.run(
            tuple(command),
            cwd=cwd,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
            timeout=timeout_seconds,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired:
        return 124, ""
    except OSError:
        return 126, ""
    return result.returncode, result.stdout


def _load_or_initialize_suite_state(
    path: Path,
    *,
    arm: str,
    stream_scopes: tuple[str, ...] = PILOT500_STREAM_SCOPES,
    stream_task_counts: Mapping[str, int] | None = None,
    schema_version: str = "taskwise_pilot500_stream_suite_state_v1",
) -> dict[str, Any]:
    if path.exists() or path.is_symlink():
        raise TaskwiseCLIError("TASKWISE_SUITE_STATE_EXISTS")
    counts = (
        {scope: PILOT500_TASKS_PER_STREAM for scope in stream_scopes}
        if stream_task_counts is None
        else dict(stream_task_counts)
    )
    if (
        not stream_scopes
        or set(counts) != set(stream_scopes)
        or any(type(value) is not int or value < 1 for value in counts.values())
    ):
        raise TaskwiseCLIError("TASKWISE_SUITE_STATE_INVALID")
    payload: dict[str, Any] = {
        "schema_version": schema_version,
        "arm": arm,
        "status": "INCOMPLETE",
        "active_scope": None,
        "stream_count": len(stream_scopes),
        "per_stream_task_count": counts,
        "total_task_count": sum(counts.values()),
        "reset_memory_between_streams": True,
        "completed_streams": 0,
        "infrastructure_failures": 0,
        "security_violations": 0,
        "evolution_update_failures": 0,
        "artifact_validation_failures": 0,
        "context_binding_violations": 0,
        "executor_failures": 0,
        "executor_failure_codes": {},
        "streams": {
            scope: {
                "action": "NOT_STARTED",
                "run_status": "MISSING",
                "resume_allowed": False,
                "failure_code": None,
                "completed": False,
                "missing": True,
            }
            for scope in stream_scopes
        },
    }
    if len(set(counts.values())) == 1:
        payload["tasks_per_stream"] = next(iter(counts.values()))
    return payload


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _write_suite_state(
    path: Path,
    state: dict[str, Any],
    *,
    create_only: bool,
) -> None:
    if state.get("status") not in _SUITE_RUN_STATUSES:
        raise TaskwiseCLIError("TASKWISE_SUITE_STATE_INVALID")
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.parent.chmod(0o700)
    payload = _canonical_bytes(state)
    if create_only:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o644,
            )
            os.fchmod(descriptor, 0o644)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = None
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            parent_descriptor = os.open(path.parent, _directory_open_flags())
            try:
                os.fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)
            return
        except FileExistsError as exc:
            raise TaskwiseCLIError("TASKWISE_SUITE_STATE_EXISTS") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
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
        parent_descriptor = os.open(path.parent, _directory_open_flags())
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
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
        evidence = _suite_state_evidence(state)
    except TaskwiseCLIError:
        evidence = {
            "run_status": "EXECUTION_FAILED",
            "resume_allowed": False,
            "failure_code": "EXECUTOR_PUBLIC_STATE_UNAVAILABLE",
            "completed": False,
            "missing": False,
        }
    # A generation is an all-or-nothing arm namespace.  Existing stream
    # evidence is immutable and can never be resumed, skipped, or stitched
    # into another suite invocation.
    return "terminal", evidence


def _executor_failure_code(value: object) -> str | None:
    if type(value) is not str:
        return None
    if value in _EXECUTOR_FAILURE_CODES:
        return value
    return _EXECUTOR_FAILURE_ALIASES.get(value)


def _closed_suite_failure_code(status: object, value: object) -> str | None:
    if status == TaskwiseRunStatusV1.SECURITY_TOOL_USE_VIOLATION.value:
        return (
            value
            if value == "EXECUTOR_SECURITY_TOOL_USE_VIOLATION"
            else "SECURITY_TOOL_USE_VIOLATION"
        )
    if status == TaskwiseRunStatusV1.TASKWISE_CONTEXT_BINDING_VIOLATION.value:
        return "TASKWISE_CONTEXT_BINDING_VIOLATION"
    if status == TaskwiseRunStatusV1.MEETING_722_TASKWISE_CONTRACT_VIOLATION.value:
        return "MEETING_722_TASKWISE_CONTRACT_VIOLATION"
    if value in _ARTIFACT_FAILURE_CODES:
        return value
    if status == TaskwiseRunStatusV1.TASKWISE_EVOLUTION_UPDATE_FAILED.value:
        return "TASKWISE_EVOLUTION_UPDATE_FAILED"
    executor_code = _executor_failure_code(value)
    if executor_code is not None:
        return executor_code
    if status == TaskwiseRunStatusV1.EXECUTION_FAILED.value:
        return "EXECUTOR_INTERNAL_ERROR"
    return None


def _executor_failure_code_from_exception(exc: Exception) -> str:
    terminal = _closed_terminal_exception_code(exc)
    if terminal is not None:
        return terminal
    if type(exc) is TaskwiseCLIError:
        code = _executor_failure_code(str(exc))
        if code is not None:
            return code
    raw_code = getattr(exc, "taskwise_failure_code", None)
    code = _executor_failure_code(raw_code)
    return code or "EXECUTOR_SUITE_ORCHESTRATION_FAILED"


def _closed_terminal_exception_code(exc: Exception) -> str | None:
    candidates = (
        str(exc) if type(exc) is TaskwiseCLIError else None,
        getattr(exc, "taskwise_failure_code", None),
        getattr(exc, "finding_code", None),
        getattr(exc, "code", None),
    )
    for candidate in candidates:
        value = getattr(candidate, "value", candidate)
        if type(value) is not str:
            continue
        if value in _SECURITY_FAILURE_CODES:
            return (
                "EXECUTOR_SECURITY_TOOL_USE_VIOLATION"
                if value.startswith("EXECUTOR_")
                else "SECURITY_TOOL_USE_VIOLATION"
            )
        if value in _CONTEXT_FAILURE_CODES:
            return "TASKWISE_CONTEXT_BINDING_VIOLATION"
        if value in _CONTRACT_FAILURE_CODES:
            return "MEETING_722_TASKWISE_CONTRACT_VIOLATION"
        if value in _ARTIFACT_FAILURE_CODES:
            return value
        if value in _EVOLUTION_FAILURE_CODES:
            return "TASKWISE_EVOLUTION_UPDATE_FAILED"
    return None


def _orchestration_failure_evidence(exc: Exception) -> dict[str, object]:
    code = _executor_failure_code_from_exception(exc)
    if code in {"EXECUTOR_SECURITY_TOOL_USE_VIOLATION", "SECURITY_TOOL_USE_VIOLATION"}:
        status = TaskwiseRunStatusV1.SECURITY_TOOL_USE_VIOLATION.value
    elif code == "TASKWISE_CONTEXT_BINDING_VIOLATION":
        status = TaskwiseRunStatusV1.TASKWISE_CONTEXT_BINDING_VIOLATION.value
    elif code == "MEETING_722_TASKWISE_CONTRACT_VIOLATION":
        status = TaskwiseRunStatusV1.MEETING_722_TASKWISE_CONTRACT_VIOLATION.value
    elif code == "TASKWISE_EVOLUTION_UPDATE_FAILED" or code in _ARTIFACT_FAILURE_CODES:
        status = TaskwiseRunStatusV1.TASKWISE_EVOLUTION_UPDATE_FAILED.value
    else:
        status = TaskwiseRunStatusV1.EXECUTION_FAILED.value
    return {
        "run_status": status,
        "resume_allowed": False,
        "failure_code": code,
        "completed": False,
        "missing": False,
    }


def _suite_state_evidence(state: dict[str, Any]) -> dict[str, object]:
    failure = state.get("failure")
    raw_failure_code = failure.get("code") if type(failure) is dict else None
    status = state.get("status")
    return {
        "run_status": status,
        "resume_allowed": state.get("resume_allowed") is True,
        "failure_code": _closed_suite_failure_code(status, raw_failure_code),
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
    raw_failure_code = (
        findings[0]
        if type(findings) is list and len(findings) == 1 and type(findings[0]) is str
        else None
    )
    return {
        "run_status": status,
        "resume_allowed": result.get("resume_allowed") is True,
        "failure_code": _closed_suite_failure_code(status, raw_failure_code),
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
    state["meeting722_contract_violations"] = sum(
        _stream_failure_class(entry) == "contract" for entry in entries
    )
    executor_codes = tuple(
        code
        for entry in entries
        if type(entry) is dict
        and (code := _executor_failure_code(entry.get("failure_code"))) is not None
    )
    state["executor_failures"] = len(executor_codes)
    state["executor_failure_codes"] = {
        code: executor_codes.count(code) for code in sorted(set(executor_codes))
    }


def _stream_failure_class(entry: object) -> str | None:
    if type(entry) is not dict:
        return "infrastructure"
    status = entry.get("run_status")
    code = entry.get("failure_code")
    if (
        status == TaskwiseRunStatusV1.SECURITY_TOOL_USE_VIOLATION.value
        or code == "EXECUTOR_SECURITY_TOOL_USE_VIOLATION"
    ):
        return "security"
    if (
        status == TaskwiseRunStatusV1.TASKWISE_CONTEXT_BINDING_VIOLATION.value
        or code == "TASKWISE_CONTEXT_BINDING_VIOLATION"
    ):
        return "context"
    if (
        status == TaskwiseRunStatusV1.MEETING_722_TASKWISE_CONTRACT_VIOLATION.value
        or code == "MEETING_722_TASKWISE_CONTRACT_VIOLATION"
    ):
        return "contract"
    if code in _ARTIFACT_FAILURE_CODES:
        return "artifact"
    if (
        status == TaskwiseRunStatusV1.TASKWISE_EVOLUTION_UPDATE_FAILED.value
        or code == "TASKWISE_EVOLUTION_UPDATE_FAILED"
    ):
        return "evolution_update"
    if _executor_failure_code(code) is not None:
        return "infrastructure"
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


def _verify_meeting722_run_contract(
    *,
    output_directory: Path,
    config: TaskwiseExperimentConfigV1,
    state: dict[str, Any],
    episodes: tuple[TaskwiseEpisodeV1, ...],
) -> str:
    """Recompute the named meeting contract before any paired statistics."""

    try:
        public_events = tuple(
            json.loads(line)
            for line in (output_directory / "public" / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line
        )
        private_evaluations = tuple(
            json.loads(line)
            for line in (output_directory / "private" / "evaluations.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line
        )
        expected = issue_meeting722_taskwise_contract_v1(
            protocol_id=(
                "taskwise_online_evolution_v1"
                if config.arm == "online"
                else "repeated_session_control_v1"
            ),
            arm=config.arm,
            run_id=config.run_name,
            stream_id=config.scope,
            expected_task_uids=tuple(episode.task.uid for episode in episodes),
            verified_task_count=len(episodes),
            public_events=public_events,
            private_evaluations=private_evaluations,
            run_state=state,
        )
        stored = json.loads(
            (output_directory / "public" / "meeting722_taskwise_contract_v1.json").read_text(
                encoding="utf-8"
            )
        )
        verified = verify_persisted_meeting722_taskwise_contract_v1(
            stored,
            expected=expected,
        )
        core_state_root = TASKWISE_STATE_ROOT / config.scope / config.run_name
        if config.arm == "control":
            if core_state_root.exists() or core_state_root.is_symlink():
                raise ValueError("control arm unexpectedly has Core state")
        elif not core_state_root.is_dir() or core_state_root.is_symlink():
            raise ValueError("online arm Core state is unavailable")
        return verified.receipt_sha256
    except Exception as exc:
        raise TaskwiseCLIError("MEETING_722_TASKWISE_CONTRACT_VIOLATION") from exc


def _security_violation_count(state: dict[str, Any]) -> int:
    return int(state.get("status") == "SECURITY_TOOL_USE_VIOLATION")


def _strict_state_counter(state: dict[str, Any], field_name: str) -> int:
    value = state.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TaskwiseCLIError("TASKWISE_RUN_EVIDENCE_INVALID")
    return value


def _artifact_validation_failure_count(state: dict[str, Any]) -> int:
    failure = state.get("failure")
    return int(isinstance(failure, dict) and failure.get("code") in _ARTIFACT_FAILURE_CODES)


def _require_user_full_run_authorization_v1() -> None:
    """Keep every paid 4009-item entry point closed until a later source change."""

    if FULL4009_AUTHORIZATION != "GRANTED" or FULL4009_EXECUTION_ALLOWED is not True:
        raise TaskwiseCLIError(FULL4009_BLOCK_CODE)


def _stream_public_evidence(
    output_directory: Path,
    *,
    expected_arm: str,
    state: dict[str, Any],
    expected_task_count: int = PILOT500_TASKS_PER_STREAM,
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
    if len(completions) != expected_task_count * 3:
        findings += 1
    if expected_arm == "control":
        if updates or any(row.get("memory") is not None for row in completions):
            findings += 1
        return (), findings
    if len(updates) != expected_task_count * 2:
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
    for task_index in range(1, expected_task_count):
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
    if command in {"compare", "compare-pilot-streams", "compare-full-streams"}:
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


def _closed_cli_error_code(error: BaseException) -> str:
    """Project arbitrary exceptions to the CLI's closed public finding set."""

    if type(error) is TaskwiseCLIError:
        code = str(error)
        if code in _CLI_PUBLIC_FAILURE_CODES:
            return code
    return "TASKWISE_CLI_INTERNAL_ERROR"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chembench4k-taskwise-online-v1")
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run-arm")
    run_parser.add_argument("--config", type=Path, required=True)
    run_parser.add_argument("--resume", action="store_true")
    run_parser.add_argument("--generation-id")
    run_parser.add_argument("--attempt-id")
    suite_parser = commands.add_parser("run-pilot-stream-suite")
    suite_parser.add_argument("--arm", choices=("control", "online"), required=True)
    suite_parser.add_argument("--generation-id")
    suite_parser.add_argument("--attempt-id")
    compare_parser = commands.add_parser("compare")
    compare_parser.add_argument("--scope", choices=_SCOPES, required=True)
    compare_parser.add_argument("--generation-id")
    compare_parser.add_argument("--attempt-id")
    compare_pilot_parser = commands.add_parser("compare-pilot-streams")
    compare_pilot_parser.add_argument("--generation-id")
    compare_pilot_parser.add_argument("--attempt-id")
    full_suite_parser = commands.add_parser("run-full-stream-suite")
    full_suite_parser.add_argument("--arm", choices=("control", "online"), required=True)
    full_suite_parser.add_argument("--generation-id")
    full_suite_parser.add_argument("--attempt-id")
    compare_full_parser = commands.add_parser("compare-full-streams")
    compare_full_parser.add_argument("--generation-id")
    compare_full_parser.add_argument("--attempt-id")
    freeze_parser = commands.add_parser("freeze-canary-receipt")
    freeze_parser.add_argument("--generation-id")
    verify_parser = commands.add_parser("verify-canary-receipt")
    verify_parser.add_argument("--generation-id")
    freeze_go_parser = commands.add_parser("freeze-pilot-go-receipt")
    freeze_go_parser.add_argument("--generation-id")
    freeze_go_parser.add_argument("--attempt-id")
    verify_go_parser = commands.add_parser("verify-pilot-go-receipt")
    verify_go_parser.add_argument("--generation-id")
    verify_go_parser.add_argument("--attempt-id")
    commands.add_parser("dry-run")
    arguments = parser.parse_args(argv)

    try:
        if arguments.command == "run-arm":
            payload = run_arm(
                arguments.config,
                resume=arguments.resume,
                generation_id=arguments.generation_id,
                attempt_id=arguments.attempt_id,
            )
        elif arguments.command == "run-pilot-stream-suite":
            payload = run_pilot500_stream_suite(
                arm=arguments.arm,
                generation_id=arguments.generation_id,
                attempt_id=arguments.attempt_id,
            )
        elif arguments.command == "compare":
            payload = compare(
                scope=arguments.scope,
                generation_id=arguments.generation_id,
                attempt_id=arguments.attempt_id,
            )
        elif arguments.command == "compare-pilot-streams":
            payload = compare_pilot500_streams(
                generation_id=arguments.generation_id,
                attempt_id=arguments.attempt_id,
            )
        elif arguments.command == "run-full-stream-suite":
            payload = run_full_stream_suite(
                arm=arguments.arm,
                generation_id=arguments.generation_id,
                attempt_id=arguments.attempt_id,
            )
        elif arguments.command == "compare-full-streams":
            payload = compare_full_streams(
                generation_id=arguments.generation_id,
                attempt_id=arguments.attempt_id,
            )
        elif arguments.command == "freeze-canary-receipt":
            payload = freeze_taskwise_paired_canary_receipt_v1(arguments.generation_id)
        elif arguments.command == "verify-canary-receipt":
            payload = verify_taskwise_pilot_receipt_v1(arguments.generation_id)
        elif arguments.command == "freeze-pilot-go-receipt":
            payload = freeze_taskwise_pilot_go_receipt_v1(
                arguments.generation_id,
                arguments.attempt_id,
            )
        elif arguments.command == "verify-pilot-go-receipt":
            payload = verify_taskwise_full_receipt_v1(
                arguments.generation_id,
                arguments.attempt_id,
            )
        else:
            payload = dry_run()
    except (TaskwiseCLIError, RuntimeError, ValueError, TypeError) as exc:
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "error_type": _closed_cli_error_code(exc),
                    "model_calls": 0,
                },
                sort_keys=True,
            ),
            file=os.sys.stderr,
        )
        return 2
    safe_payload = _safe_cli_payload(arguments.command, payload)
    print(json.dumps(safe_payload, indent=2, sort_keys=True))
    if arguments.command in {
        "run-arm",
        "run-pilot-stream-suite",
        "run-full-stream-suite",
    }:
        return 0 if safe_payload.get("status") == "COMPLETED" else 2
    return 2 if safe_payload.get("status") == "BLOCKED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
