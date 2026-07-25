"""Explicitly unpaired online-only pilot execution.

This module exists for infrastructure/runtime observation when an operator
deliberately chooses not to execute a source-matched control arm.  It reuses
the frozen online stream configs, manifests, executor, Core evolution port,
and runner, but writes into a disjoint namespace that the paired comparison
and GO-receipt code never consumes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from openevo_chembench.taskwise_canary_receipt_v1 import (
    TaskwisePilotAuthorizationV1,
)
from openevo_chembench.taskwise_cli_v1 import (
    CONFIG_ROOT,
    REPOSITORY_ROOT,
    _ClosedExecutorFailureAdapterV1,
    _build_episodes,
    _build_run_config,
    _orchestration_failure_evidence,
    _resolve_workspace_path,
    _suite_result_evidence,
    _update_suite_totals,
    _verify_static_inputs,
    _write_suite_state,
    build_default_taskwise_core_port_v1,
    build_default_taskwise_executor_v1,
    require_taskwise_pilot_authorization_v1,
    verify_taskwise_codex_policy_v1,
    verify_taskwise_source_gate_v1,
)
from openevo_chembench.taskwise_config_v1 import (
    TaskwiseExperimentConfigV1,
    load_taskwise_config_v1,
)
from openevo_chembench.taskwise_online_runner_v1 import (
    TaskwiseOnlineRunnerV1,
)
from openevo_chembench.taskwise_sampling_v1 import (
    PILOT500_STREAM_SCOPES,
    PILOT500_TASKS_PER_STREAM,
)


ONLINE_ONLY_CLASSIFICATION = "UNPAIRED_ONLINE_ONLY_NOT_FOR_PAIRED_INFERENCE"
ONLINE_ONLY_SCHEMA_V1 = "taskwise_unpaired_online_only_pilot500_suite_v1"
ONLINE_ONLY_AUTHORITY_SCHEMA_V1 = "taskwise_unpaired_online_only_authority_v1"
ONLINE_ONLY_STREAM_MARKER_SCHEMA_V1 = "taskwise_unpaired_online_only_stream_marker_v1"
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,79}")
_RUNS_DIRECTORY = "unpaired_online_only_pilot500_runs"

StreamRunnerV1 = Callable[
    [Path, TaskwiseExperimentConfigV1, str, TaskwisePilotAuthorizationV1],
    dict[str, object],
]


class TaskwiseOnlineOnlyError(RuntimeError):
    """Closed online-only orchestration failure."""


def validate_online_only_run_id_v1(run_id: str) -> str:
    """Require an explicit bounded identifier for one immutable online-only run."""

    if type(run_id) is not str or _RUN_ID.fullmatch(run_id) is None:
        raise ValueError("online-only run id is invalid")
    return run_id


def online_only_run_root_v1(repository_root: Path, run_id: str) -> Path:
    """Return the namespace intentionally excluded from paired attempts."""

    if not isinstance(repository_root, Path):
        raise TypeError("repository_root must be pathlib.Path")
    return (
        repository_root.resolve()
        / "results"
        / "chembench4k_taskwise_online_v1"
        / _RUNS_DIRECTORY
        / validate_online_only_run_id_v1(run_id)
    )


def online_only_runtime_config_v1(
    template: TaskwiseExperimentConfigV1,
    *,
    run_id: str,
) -> TaskwiseExperimentConfigV1:
    """Bind one frozen online template to the disjoint online-only namespace."""

    if type(template) is not TaskwiseExperimentConfigV1:
        raise TypeError("template must be exact TaskwiseExperimentConfigV1")
    if template.arm != "online" or template.scope not in PILOT500_STREAM_SCOPES:
        raise ValueError("online-only runtime requires an online pilot stream template")
    validated = validate_online_only_run_id_v1(run_id)
    identity = hashlib.sha256(validated.encode("utf-8")).hexdigest()[:16]
    return replace(
        template,
        run_name=f"onlineonly_{template.scope}_{identity}",
        output_directory=(
            "OpenEvo/results/chembench4k_taskwise_online_v1/"
            f"{_RUNS_DIRECTORY}/{validated}/{template.scope}/online"
        ),
    )


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _create_file_exclusive(path: Path, payload: dict[str, Any], *, mode: int) -> None:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            mode,
        )
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(_canonical_bytes(payload))
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _create_online_only_authority(
    repository_root: Path,
    *,
    run_id: str,
    authorization: TaskwisePilotAuthorizationV1,
    source_commit: str,
) -> Path:
    root = online_only_run_root_v1(repository_root, run_id)
    parent = root.parent
    parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if parent.resolve() != parent:
        raise TaskwiseOnlineOnlyError("ONLINE_ONLY_NAMESPACE_ATTESTATION_FAILED")
    try:
        root.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise TaskwiseOnlineOnlyError("ONLINE_ONLY_RUN_EXISTS") from exc
    root.chmod(0o700)
    authority = {
        "schema_version": ONLINE_ONLY_AUTHORITY_SCHEMA_V1,
        "classification": ONLINE_ONLY_CLASSIFICATION,
        "paired_inference_allowed": False,
        "paired_comparison_allowed": False,
        "pilot_go_receipt_allowed": False,
        "run_id": run_id,
        "source_commit": source_commit,
        "paired_canary_generation_id": authorization.paired_canary_generation_id,
        "canary_receipt_sha256": authorization.receipt_sha256,
        "pilot_binding_sha256": authorization.pilot_binding_sha256,
    }
    try:
        _create_file_exclusive(root / "online_only_authority.json", authority, mode=0o600)
    except Exception:
        try:
            root.rmdir()
        except OSError:
            pass
        raise
    return root


def _write_stream_marker(
    output_directory: Path,
    *,
    run_id: str,
    source_commit: str,
    authorization: TaskwisePilotAuthorizationV1,
    config: TaskwiseExperimentConfigV1,
) -> None:
    marker = {
        "schema_version": ONLINE_ONLY_STREAM_MARKER_SCHEMA_V1,
        "classification": ONLINE_ONLY_CLASSIFICATION,
        "paired_inference_allowed": False,
        "paired_comparison_allowed": False,
        "pilot_go_receipt_allowed": False,
        "run_id": run_id,
        "scope": config.scope,
        "source_commit": source_commit,
        "paired_canary_generation_id": authorization.paired_canary_generation_id,
        "online_config_sha256": config.config_sha256(),
    }
    _create_file_exclusive(
        output_directory / "UNPAIRED_ONLINE_ONLY.json",
        marker,
        mode=0o644,
    )


def _run_online_only_stream(
    config_path: Path,
    runtime_config: TaskwiseExperimentConfigV1,
    run_id: str,
    authorization: TaskwisePilotAuthorizationV1,
) -> dict[str, object]:
    """Execute one online stream without granting paired statistical authority."""

    template = load_taskwise_config_v1(config_path.resolve())
    source_commit = verify_taskwise_source_gate_v1(template)
    loader, manifest, paired_templates = _verify_static_inputs(template, config_path)
    episodes = _build_episodes(loader, scope=template.scope)
    if len(episodes) != manifest.item_count:
        raise TaskwiseOnlineOnlyError("TASKWISE_EPISODE_COUNT_MISMATCH")
    output_directory = _resolve_workspace_path(
        runtime_config.output_directory,
        field_name="output_directory",
        must_exist=False,
    )
    if output_directory.exists():
        raise TaskwiseOnlineOnlyError("ONLINE_ONLY_STREAM_OUTPUT_EXISTS")
    verify_taskwise_codex_policy_v1(runtime_config)
    executor = _ClosedExecutorFailureAdapterV1(build_default_taskwise_executor_v1(runtime_config))
    core_port = None
    try:
        core_port = build_default_taskwise_core_port_v1(runtime_config)
        runner = TaskwiseOnlineRunnerV1(
            config=_build_run_config(
                config=runtime_config,
                output_directory=output_directory,
                paired_configs=paired_templates,
                current_commit=source_commit,
                dataset_sha256=loader.manifest.combined_sha256,
                task_manifest_sha256=manifest.public_sha256,
            ),
            episodes=episodes,
            executor=executor,
            core_update_port=core_port,
            resume=False,
        )
        result = runner.run()
    finally:
        close_core = getattr(core_port, "close", None)
        if callable(close_core):
            close_core()
        close_executor = getattr(executor, "close", None)
        if callable(close_executor):
            close_executor()
    _write_stream_marker(
        output_directory,
        run_id=run_id,
        source_commit=source_commit,
        authorization=authorization,
        config=template,
    )
    payload = result.to_public_dict()
    payload["execution_classification"] = ONLINE_ONLY_CLASSIFICATION
    payload["paired_inference_allowed"] = False
    return payload


def run_online_only_pilot500_v1(
    *,
    run_id: str,
    repository_root: Path = REPOSITORY_ROOT,
    stream_runner: StreamRunnerV1 | None = None,
) -> dict[str, object]:
    """Run ten independent online streams in an explicitly unpaired namespace."""

    validated_run_id = validate_online_only_run_id_v1(run_id)
    authorization = require_taskwise_pilot_authorization_v1()
    preflight: list[tuple[Path, TaskwiseExperimentConfigV1]] = []
    source_commits: set[str] = set()
    for scope in PILOT500_STREAM_SCOPES:
        config_path = CONFIG_ROOT / f"online_{scope}_taskwise_online_v1.yaml"
        template = load_taskwise_config_v1(config_path)
        source_commits.add(verify_taskwise_source_gate_v1(template))
        _verify_static_inputs(template, config_path)
        preflight.append((config_path, template))
    if source_commits != {authorization.source_commit}:
        raise TaskwiseOnlineOnlyError("ONLINE_ONLY_SOURCE_AUTHORITY_MISMATCH")
    source_commit = next(iter(source_commits))
    root = _create_online_only_authority(
        repository_root,
        run_id=validated_run_id,
        authorization=authorization,
        source_commit=source_commit,
    )
    state_path = root / "online_only_suite_state.json"
    state: dict[str, Any] = {
        "schema_version": ONLINE_ONLY_SCHEMA_V1,
        "classification": ONLINE_ONLY_CLASSIFICATION,
        "paired_inference_allowed": False,
        "paired_comparison_allowed": False,
        "pilot_go_receipt_allowed": False,
        "run_id": validated_run_id,
        "arm": "online",
        "status": "INCOMPLETE",
        "active_scope": None,
        "stream_count": len(PILOT500_STREAM_SCOPES),
        "tasks_per_stream": PILOT500_TASKS_PER_STREAM,
        "per_stream_task_count": {
            scope: PILOT500_TASKS_PER_STREAM for scope in PILOT500_STREAM_SCOPES
        },
        "total_task_count": (len(PILOT500_STREAM_SCOPES) * PILOT500_TASKS_PER_STREAM),
        "reset_memory_between_streams": True,
        "completed_streams": 0,
        "infrastructure_failures": 0,
        "security_violations": 0,
        "evolution_update_failures": 0,
        "artifact_validation_failures": 0,
        "context_binding_violations": 0,
        "executor_failures": 0,
        "executor_failure_codes": {},
        "source_commit": source_commit,
        "paired_canary_generation_id": authorization.paired_canary_generation_id,
        "canary_receipt_sha256": authorization.receipt_sha256,
        "pilot_binding_sha256": authorization.pilot_binding_sha256,
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
    persisted = False

    def persist() -> None:
        nonlocal persisted
        _write_suite_state(state_path, state, create_only=not persisted)
        persisted = True

    runner = stream_runner or _run_online_only_stream
    for config_path, template in preflight:
        scope = template.scope
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
        persist()
        runtime_config = online_only_runtime_config_v1(
            template,
            run_id=validated_run_id,
        )
        try:
            result = runner(
                config_path,
                runtime_config,
                validated_run_id,
                authorization,
            )
            evidence = _suite_result_evidence(result)
        except Exception as exc:
            evidence = _orchestration_failure_evidence(exc)
            state["streams"][scope] = {
                "action": "ORCHESTRATION_ERROR",
                **evidence,
            }
            state["active_scope"] = None
            state["status"] = "FAILED"
            _update_suite_totals(state)
            persist()
            raise
        state["streams"][scope] = {"action": "STARTED", **evidence}
        if not evidence["completed"]:
            state["active_scope"] = None
            state["status"] = "FAILED"
            _update_suite_totals(state)
            persist()
            raise TaskwiseOnlineOnlyError("ONLINE_ONLY_SUITE_TERMINAL_FAILURE")
        state["streams"][scope]["action"] = "COMPLETED"
        state["active_scope"] = None
        _update_suite_totals(state)
        persist()
    state["status"] = "COMPLETED"
    _update_suite_totals(state)
    persist()
    return state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chembench-taskwise-online-only-v1")
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run-pilot500")
    run_parser.add_argument("--run-id", required=True)
    arguments = parser.parse_args(argv)
    try:
        payload = run_online_only_pilot500_v1(run_id=arguments.run_id)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "classification": ONLINE_ONLY_CLASSIFICATION,
                    "error_type": type(exc).__name__,
                    "model_calls": 0,
                },
                sort_keys=True,
            ),
            file=os.sys.stderr,
        )
        return 2
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "ONLINE_ONLY_CLASSIFICATION",
    "TaskwiseOnlineOnlyError",
    "main",
    "online_only_run_root_v1",
    "online_only_runtime_config_v1",
    "run_online_only_pilot500_v1",
    "validate_online_only_run_id_v1",
]
