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

from openevo_chembench.chembench4k_models import CHEMBENCH4K_CATEGORIES
from openevo_chembench.local_codex_executor import (
    load_taskwise_success_receipts_v1,
)
from openevo_chembench.taskwise_online_canary_receipt_v1 import (
    OnlineCanaryPilotAuthorizationV1,
    require_online_canary_authorization_v1,
)
from openevo_chembench.taskwise_cli_v1 import (
    CONFIG_ROOT,
    PACKAGE_ROOT,
    REPOSITORY_ROOT,
    _ClosedExecutorFailureAdapterV1,
    _build_episodes,
    _build_run_config,
    _executor_policy_sha256,
    _orchestration_failure_evidence,
    _read_public_run_state,
    _resolve_workspace_path,
    _suite_result_evidence,
    _suite_state_evidence,
    _stream_public_evidence,
    _update_suite_totals,
    _verify_meeting722_run_contract,
    _verify_static_inputs,
    _write_suite_state,
    TaskwiseCLIError,
    build_default_taskwise_core_port_v1,
    build_default_taskwise_executor_v1,
    verify_taskwise_codex_policy_v1,
    verify_taskwise_source_gate_v1,
)
from openevo_chembench.taskwise_config_v1 import (
    TaskwiseExperimentConfigV1,
    load_taskwise_config_v1,
)
from openevo_chembench.taskwise_online_runner_v1 import (
    TaskwiseMemoryPublicMetricsV1,
    TaskwiseOnlineRunnerV1,
    load_private_taskwise_results_v1,
)
from openevo_chembench.taskwise_sampling_v1 import (
    PILOT500_STREAM_SCOPES,
    PILOT500_TASKS_PER_STREAM,
)


ONLINE_ONLY_CLASSIFICATION = "UNPAIRED_ONLINE_ONLY_NOT_FOR_PAIRED_INFERENCE"
ONLINE_ONLY_SCHEMA_V1 = "taskwise_unpaired_online_only_pilot500_suite_v1"
ONLINE_ONLY_AUTHORITY_SCHEMA_V1 = "taskwise_unpaired_online_only_authority_v1"
ONLINE_ONLY_STREAM_MARKER_SCHEMA_V1 = "taskwise_unpaired_online_only_stream_marker_v1"
ONLINE_ONLY_REPORT_SCHEMA_V1 = "taskwise_online_only_pilot500_descriptive_report_v1"
ONLINE_ONLY_RECOVERY_RECEIPT_SCHEMA_V1 = "taskwise_online_only_pilot500_report_recovery_receipt_v1"
ONLINE_ONLY_REPORT_LABELS = (
    "ONLINE_TASKWISE_EVOLUTION",
    "TEST_TIME_ADAPTATION",
    "ONLINE_ONLY_PILOT500",
    ONLINE_ONLY_CLASSIFICATION,
    "NON_STANDARD_CHEMBENCH4K_PROTOCOL",
    "NOT_A_STANDARD_LEADERBOARD_SCORE",
    "DESCRIPTIVE_ONLINE_ONLY_RESULT",
    "NO_CONTROL_ARM",
    "NO_CAUSAL_CONTROL_COMPARISON",
    "NO_PAIRED_PERFORMANCE_CLAIM",
)
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,79}")
_RUNS_DIRECTORY = "unpaired_online_only_pilot500_runs"
_RECOVERED_REPORTS_DIRECTORY = "unpaired_online_only_pilot500_recovered_reports"

StreamRunnerV1 = Callable[
    [Path, TaskwiseExperimentConfigV1, str, OnlineCanaryPilotAuthorizationV1],
    dict[str, object],
]


class TaskwiseOnlineOnlyError(RuntimeError):
    """Closed online-only orchestration failure."""


_EXPECTED_TASKS_PER_STREAM = PILOT500_TASKS_PER_STREAM
_EXPECTED_COMPLETIONS_PER_STREAM = PILOT500_TASKS_PER_STREAM * 3
_EXPECTED_UPDATES_PER_STREAM = PILOT500_TASKS_PER_STREAM * 2
_EXPECTED_STREAM_COUNT = len(PILOT500_STREAM_SCOPES)
_OBSERVED_COUNT_FIELDS = (
    "completed_tasks",
    "completion_count",
    "session_attempt_count",
    "update_count",
    "core_job_count",
    "core_artifact_count",
    "context_resolution_count",
)
_EXPECTED_SUITE_COUNTS = {
    "completed_tasks": _EXPECTED_STREAM_COUNT * _EXPECTED_TASKS_PER_STREAM,
    "completion_count": _EXPECTED_STREAM_COUNT * _EXPECTED_COMPLETIONS_PER_STREAM,
    "session_attempt_count": _EXPECTED_STREAM_COUNT * _EXPECTED_COMPLETIONS_PER_STREAM,
    "update_count": _EXPECTED_STREAM_COUNT * _EXPECTED_UPDATES_PER_STREAM,
    "core_job_count": _EXPECTED_STREAM_COUNT * _EXPECTED_UPDATES_PER_STREAM,
    "core_artifact_count": _EXPECTED_STREAM_COUNT * _EXPECTED_UPDATES_PER_STREAM,
    "context_resolution_count": _EXPECTED_STREAM_COUNT * _EXPECTED_UPDATES_PER_STREAM,
}


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


def online_only_recovered_report_root_v1(
    repository_root: Path,
    *,
    run_id: str,
    reporter_source_commit: str,
) -> Path:
    """Return a report namespace disjoint from the immutable failed run."""

    if (
        not isinstance(repository_root, Path)
        or type(reporter_source_commit) is not str
        or re.fullmatch(r"[0-9a-f]{40}", reporter_source_commit) is None
    ):
        raise ValueError("online-only recovered report identity is invalid")
    return (
        repository_root.resolve()
        / "results"
        / "chembench4k_taskwise_online_v1"
        / _RECOVERED_REPORTS_DIRECTORY
        / validate_online_only_run_id_v1(run_id)
        / reporter_source_commit
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


def _sha256_payload(payload: object) -> str:
    """Hash a JSON-safe, content-free evidence payload deterministically."""

    encoded = (
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
    authorization: OnlineCanaryPilotAuthorizationV1,
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
        "classification": list(ONLINE_ONLY_REPORT_LABELS),
        "primary_classification": ONLINE_ONLY_CLASSIFICATION,
        "paired_inference_allowed": False,
        "paired_comparison_allowed": False,
        "pilot_go_receipt_allowed": False,
        "run_id": run_id,
        "source_commit": source_commit,
        "online_canary_generation_id": authorization.online_canary_generation_id,
        "canary_receipt_sha256": authorization.receipt_sha256,
        "online_pilot_binding_sha256": authorization.online_pilot_binding_sha256,
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
    authorization: OnlineCanaryPilotAuthorizationV1,
    config: TaskwiseExperimentConfigV1,
) -> None:
    marker = {
        "schema_version": ONLINE_ONLY_STREAM_MARKER_SCHEMA_V1,
        "classification": list(ONLINE_ONLY_REPORT_LABELS),
        "primary_classification": ONLINE_ONLY_CLASSIFICATION,
        "paired_inference_allowed": False,
        "paired_comparison_allowed": False,
        "pilot_go_receipt_allowed": False,
        "run_id": run_id,
        "scope": config.scope,
        "source_commit": source_commit,
        "online_canary_generation_id": authorization.online_canary_generation_id,
        "online_config_sha256": config.config_sha256(),
    }
    _create_file_exclusive(
        output_directory / "UNPAIRED_ONLINE_ONLY.json",
        marker,
        mode=0o644,
    )


def _authorization_identity(
    authorization: OnlineCanaryPilotAuthorizationV1,
) -> tuple[str, str, str, str]:
    values = (
        getattr(authorization, "source_commit", None),
        getattr(authorization, "online_canary_generation_id", None),
        getattr(authorization, "receipt_sha256", None),
        getattr(authorization, "online_pilot_binding_sha256", None),
    )
    if any(type(value) is not str or not value for value in values):
        raise TaskwiseOnlineOnlyError("ONLINE_ONLY_CANARY_AUTHORITY_INVALID")
    return values


def _require_current_authorization(
    expected: OnlineCanaryPilotAuthorizationV1,
) -> OnlineCanaryPilotAuthorizationV1:
    current = require_online_canary_authorization_v1(PACKAGE_ROOT)
    if _authorization_identity(current) != _authorization_identity(expected):
        raise TaskwiseOnlineOnlyError("ONLINE_ONLY_CANARY_AUTHORITY_MISMATCH")
    return current


def _completed_stream_counts(result: dict[str, object]) -> dict[str, int]:
    observed = _observed_stream_counts(result)
    expected = {
        "planned_tasks": _EXPECTED_TASKS_PER_STREAM,
        "completed_tasks": _EXPECTED_TASKS_PER_STREAM,
        "completion_count": _EXPECTED_COMPLETIONS_PER_STREAM,
        "session_attempt_count": _EXPECTED_COMPLETIONS_PER_STREAM,
        "update_count": _EXPECTED_UPDATES_PER_STREAM,
        "core_job_count": _EXPECTED_UPDATES_PER_STREAM,
        "core_artifact_count": _EXPECTED_UPDATES_PER_STREAM,
        "context_resolution_count": _EXPECTED_UPDATES_PER_STREAM,
        "context_binding_violation_count": 0,
    }
    if (
        result.get("status") != "COMPLETED"
        or result.get("resume_allowed") is not False
        or result.get("finding_codes") != []
        or any(observed.get(field) != value for field, value in expected.items())
    ):
        raise TaskwiseCLIError("EXECUTOR_OUTPUT_INVALID")
    return expected


def _observed_stream_counts(result: object) -> dict[str, int]:
    """Validate and retain content-free partial counters from any terminal result."""

    if type(result) is not dict:
        raise TaskwiseCLIError("EXECUTOR_OUTPUT_INVALID")
    memory = result.get("memory_aggregate")
    if (
        result.get("schema_version") != "taskwise_online_run_result_v1"
        or result.get("protocol_id") != "taskwise_online_evolution_v1"
        or result.get("standard_chembench4k_score_claimed") is not False
        or result.get("arm") != "online"
        or result.get("resume_allowed") is not False
        or type(memory) is not dict
    ):
        raise TaskwiseCLIError("EXECUTOR_OUTPUT_INVALID")
    fields = (
        "planned_tasks",
        *_OBSERVED_COUNT_FIELDS,
        "context_binding_violation_count",
    )
    if any(type(result.get(field)) is not int or result[field] < 0 for field in fields):
        raise TaskwiseCLIError("EXECUTOR_OUTPUT_INVALID")
    observed = {field: int(result[field]) for field in fields}
    planned = observed["planned_tasks"]
    completed = observed["completed_tasks"]
    completions = observed["completion_count"]
    attempts = observed["session_attempt_count"]
    updates = observed["update_count"]
    findings = result.get("finding_codes")
    if (
        planned != _EXPECTED_TASKS_PER_STREAM
        or completed > planned
        or completions > _EXPECTED_COMPLETIONS_PER_STREAM
        or attempts > _EXPECTED_COMPLETIONS_PER_STREAM
        or completions > attempts
        or attempts > completions + 1
        or completions < completed * 3
        or completions > min(_EXPECTED_COMPLETIONS_PER_STREAM, completed * 3 + 3)
        or updates > _EXPECTED_UPDATES_PER_STREAM
        or updates < completed * 2
        or updates > min(_EXPECTED_UPDATES_PER_STREAM, completed * 2 + 2)
        or observed["core_job_count"] != updates
        or observed["core_artifact_count"] != updates
        or observed["context_resolution_count"] != updates
        or memory.get("approved_artifact_count") != updates
        or type(findings) is not list
        or any(type(code) is not str for code in findings)
        or (result.get("status") == "COMPLETED" and findings != [])
        or (result.get("status") != "COMPLETED" and len(findings) != 1)
    ):
        raise TaskwiseCLIError("EXECUTOR_OUTPUT_INVALID")
    return observed


def _online_only_stream_result_evidence(
    result: object,
) -> dict[str, object]:
    evidence = _suite_result_evidence(result)
    observed = _observed_stream_counts(result)
    evidence.update(observed)
    if evidence["completed"]:
        if type(result) is not dict:
            raise TaskwiseCLIError("EXECUTOR_OUTPUT_INVALID")
        _completed_stream_counts(result)
    return evidence


def _close_online_only_resources(
    core_port: object,
    executor: object,
) -> tuple[BaseException, ...]:
    failures: list[BaseException] = []
    for resource in (core_port, executor):
        close = getattr(resource, "close", None)
        if not callable(close):
            continue
        try:
            close()
        except BaseException as exc:
            failures.append(exc)
    return tuple(failures)


def _raise_primary_or_cleanup(
    primary: BaseException | None,
    cleanup_failures: tuple[BaseException, ...],
) -> None:
    if primary is not None:
        raise primary
    if cleanup_failures:
        raise TaskwiseCLIError("EXECUTOR_CLEANUP_FAILED") from cleanup_failures[0]


def _failure_evidence(
    output_directory: Path | None,
    exc: BaseException,
) -> dict[str, object]:
    if output_directory is not None and output_directory.is_dir():
        try:
            state = _read_public_run_state(output_directory, expected_arm="online")
            evidence = _suite_state_evidence(state)
        except TaskwiseCLIError:
            pass
        else:
            if not evidence["completed"]:
                for field in _OBSERVED_COUNT_FIELDS:
                    value = state.get(field)
                    if type(value) is int and value >= 0:
                        evidence[field] = value
                return evidence
    if isinstance(exc, Exception):
        return _orchestration_failure_evidence(exc)
    return {
        "run_status": "EXECUTION_FAILED",
        "resume_allowed": False,
        "failure_code": "EXECUTOR_SUITE_ORCHESTRATION_FAILED",
        "completed": False,
        "missing": False,
    }


def _update_online_only_suite_totals(state: dict[str, Any]) -> None:
    _update_suite_totals(state)
    entries = tuple(state["streams"].values())
    for field in _OBSERVED_COUNT_FIELDS:
        state[field] = sum(
            value
            for entry in entries
            if type(entry) is dict and type(value := entry.get(field)) is int and value >= 0
        )


def _verify_completed_suite_totals(state: dict[str, Any]) -> None:
    if (
        state.get("completed_streams") != _EXPECTED_STREAM_COUNT
        or state.get("total_task_count") != _EXPECTED_SUITE_COUNTS["completed_tasks"]
        or any(state.get(field) != expected for field, expected in _EXPECTED_SUITE_COUNTS.items())
    ):
        raise TaskwiseOnlineOnlyError("ONLINE_ONLY_SUITE_TOTAL_MISMATCH")


def _public_cli_failure_counts(repository_root: Path, run_id: str) -> dict[str, int]:
    try:
        state_path = online_only_run_root_v1(repository_root, run_id)
        raw = (state_path / "online_only_suite_state.json").read_bytes()
        state = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return {}
    if (
        type(state) is not dict
        or raw != _canonical_bytes(state)
        or state.get("schema_version") != ONLINE_ONLY_SCHEMA_V1
        or state.get("run_id") != run_id
    ):
        return {}
    observed: dict[str, int] = {}
    for field in _OBSERVED_COUNT_FIELDS:
        value = state.get(field)
        if type(value) is int and value >= 0:
            observed[f"observed_{field}"] = value
    return observed


def _run_online_only_stream(
    config_path: Path,
    runtime_config: TaskwiseExperimentConfigV1,
    run_id: str,
    authorization: OnlineCanaryPilotAuthorizationV1,
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
    core_port: object = None
    result: object = None
    primary_failure: BaseException | None = None
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
    except BaseException as exc:
        primary_failure = exc
    finally:
        cleanup_failures = _close_online_only_resources(core_port, executor)
    _raise_primary_or_cleanup(primary_failure, cleanup_failures)
    if result is None:
        raise TaskwiseCLIError("EXECUTOR_OUTPUT_MISSING")
    _write_stream_marker(
        output_directory,
        run_id=run_id,
        source_commit=source_commit,
        authorization=authorization,
        config=template,
    )
    payload = result.to_public_dict()
    payload["execution_classification"] = ONLINE_ONLY_CLASSIFICATION
    payload["execution_classifications"] = list(ONLINE_ONLY_REPORT_LABELS)
    payload["paired_inference_allowed"] = False
    return payload


def _read_canonical_public_events(path: Path) -> tuple[dict[str, Any], ...]:
    try:
        lines = path.read_bytes().splitlines(keepends=True)
        rows = tuple(json.loads(line) for line in lines)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TaskwiseOnlineOnlyError("ONLINE_ONLY_PUBLIC_EVIDENCE_INVALID") from exc
    if any(
        type(row) is not dict or line != _canonical_bytes(row) for line, row in zip(lines, rows)
    ):
        raise TaskwiseOnlineOnlyError("ONLINE_ONLY_PUBLIC_EVIDENCE_INVALID")
    return rows


def _group_online_rounds(
    rows: tuple[object, ...],
) -> dict[int, dict[int, object]]:
    if len(rows) != _EXPECTED_COMPLETIONS_PER_STREAM:
        raise TaskwiseOnlineOnlyError("ONLINE_ONLY_PRIVATE_EVIDENCE_INVALID")
    grouped: dict[int, dict[int, object]] = {}
    task_uids: dict[int, str] = {}
    categories: dict[int, str] = {}
    for row in rows:
        if (
            getattr(row, "arm", None) != "online"
            or type(getattr(row, "task_index", None)) is not int
            or getattr(row, "round_index", None) not in (0, 1, 2)
        ):
            raise TaskwiseOnlineOnlyError("ONLINE_ONLY_PRIVATE_EVIDENCE_INVALID")
        task_index = row.task_index
        if not 0 <= task_index < _EXPECTED_TASKS_PER_STREAM:
            raise TaskwiseOnlineOnlyError("ONLINE_ONLY_PRIVATE_EVIDENCE_INVALID")
        if task_index in task_uids and task_uids[task_index] != row.task_uid:
            raise TaskwiseOnlineOnlyError("ONLINE_ONLY_PRIVATE_EVIDENCE_INVALID")
        if task_index in categories and categories[task_index] != row.category:
            raise TaskwiseOnlineOnlyError("ONLINE_ONLY_PRIVATE_EVIDENCE_INVALID")
        task_uids[task_index] = row.task_uid
        categories[task_index] = row.category
        rounds = grouped.setdefault(task_index, {})
        if row.round_index in rounds:
            raise TaskwiseOnlineOnlyError("ONLINE_ONLY_PRIVATE_EVIDENCE_INVALID")
        rounds[row.round_index] = row
    if (
        set(grouped) != set(range(_EXPECTED_TASKS_PER_STREAM))
        or len(set(task_uids.values())) != _EXPECTED_TASKS_PER_STREAM
        or any(set(rounds) != {0, 1, 2} for rounds in grouped.values())
    ):
        raise TaskwiseOnlineOnlyError("ONLINE_ONLY_PRIVATE_EVIDENCE_INVALID")
    return grouped


def _round_accuracy(grouped: dict[int, dict[int, object]], round_index: int) -> float:
    return (
        sum(bool(grouped[index][round_index].correct) for index in grouped)
        / _EXPECTED_TASKS_PER_STREAM
    )


def _category_metrics(grouped: dict[int, dict[int, object]]) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for category in CHEMBENCH4K_CATEGORIES:
        tasks = [rounds for rounds in grouped.values() if rounds[0].category == category]
        if not tasks:
            continue
        result[category] = {
            "task_count": len(tasks),
            "round_0_accuracy": sum(row[0].correct for row in tasks) / len(tasks),
            "round_1_accuracy": sum(row[1].correct for row in tasks) / len(tasks),
            "round_2_accuracy": sum(row[2].correct for row in tasks) / len(tasks),
            "wrong_to_correct_0_to_1": sum(
                (not row[0].correct) and row[1].correct for row in tasks
            ),
            "wrong_to_correct_1_to_2": sum(
                (not row[1].correct) and row[2].correct for row in tasks
            ),
            "wrong_to_correct_0_to_2": sum(
                (not row[0].correct) and row[2].correct for row in tasks
            ),
            "correct_to_wrong_0_to_1": sum(
                row[0].correct and (not row[1].correct) for row in tasks
            ),
            "correct_to_wrong_1_to_2": sum(
                row[1].correct and (not row[2].correct) for row in tasks
            ),
        }
    return result


def _verify_stream_executor_success_evidence(
    *,
    package_root: Path,
    config: TaskwiseExperimentConfigV1,
    completions: tuple[dict[str, Any], ...],
) -> tuple[int, str]:
    """Bind every public completion to one private, content-free success receipt."""

    if (
        not isinstance(package_root, Path)
        or type(config) is not TaskwiseExperimentConfigV1
        or config.arm != "online"
        or len(completions) != _EXPECTED_COMPLETIONS_PER_STREAM
    ):
        raise TaskwiseOnlineOnlyError("ONLINE_ONLY_EXECUTOR_EVIDENCE_INVALID")
    try:
        expected_session_ids = tuple(str(row["session_id"]) for row in completions)
        if any(
            type(row.get("session_id")) is not str
            or type(row.get("task_uid")) is not str
            or type(row.get("task_ordinal")) is not int
            or type(row.get("round_index")) is not int
            or type(row.get("transcript_reference")) is not str
            for row in completions
        ):
            raise ValueError
        receipt_root = (
            package_root
            / "state"
            / "taskwise_online_v1"
            / "private_executor_events"
            / config.scope
            / config.run_name
            / config.arm
        )
        receipts = load_taskwise_success_receipts_v1(
            receipt_root,
            expected_session_ids=expected_session_ids,
        )
        by_session = {item.session_id: item for item in receipts}
        policy_sha256 = _executor_policy_sha256(config)
        for row in completions:
            metadata = row.get("runtime_metadata")
            if (
                type(metadata) is not dict
                or metadata.get("model") != config.model
                or metadata.get("codex_cli_version") != config.codex_cli_version
                or metadata.get("harness") != "codex_cli"
                or metadata.get("execution_backend") != "local_codex_cli"
            ):
                raise ValueError
            receipt = by_session[str(row["session_id"])]
            if (
                receipt.run_id != config.run_name
                or receipt.task_uid != row["task_uid"]
                or receipt.task_index != row["task_ordinal"]
                or receipt.round_index != row["round_index"]
                or receipt.codex_cli_version != config.codex_cli_version
                or receipt.model != config.model
                or receipt.executor_policy_sha256 != policy_sha256
                or row["transcript_reference"]
                != f"local-codex-jsonl:sha256:{receipt.event_stream_sha256}"
            ):
                raise ValueError
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise TaskwiseOnlineOnlyError("ONLINE_ONLY_EXECUTOR_EVIDENCE_INVALID") from exc
    payloads = [item.to_payload() for item in receipts]
    return len(receipts), _sha256_payload(payloads)


def _build_online_only_descriptive_report(
    *,
    run_id: str,
    source_commit: str,
    preflight: list[tuple[Path, TaskwiseExperimentConfigV1]],
    suite_state: dict[str, Any],
) -> dict[str, object]:
    """Recompute a content-free online-only report from all ten frozen streams."""

    per_stream: list[dict[str, object]] = []
    all_grouped: list[dict[int, dict[int, object]]] = []
    all_task_uids: set[str] = set()
    all_artifact_ids: set[str] = set()
    all_job_ids: set[str] = set()
    manifest_hashes: dict[str, str] = {}
    dataset_hashes: set[str] = set()
    contract_receipts: dict[str, str] = {}
    executor_receipt_digests: dict[str, str] = {}
    executor_receipt_count = 0
    for config_path, template in preflight:
        runtime = online_only_runtime_config_v1(template, run_id=run_id)
        output = _resolve_workspace_path(
            runtime.output_directory,
            field_name="output_directory",
            must_exist=True,
        )
        state = _read_public_run_state(output, expected_arm="online")
        result_like = {
            "schema_version": "taskwise_online_run_result_v1",
            "protocol_id": "taskwise_online_evolution_v1",
            "standard_chembench4k_score_claimed": False,
            "status": state["status"],
            "arm": "online",
            "planned_tasks": state["planned_tasks"],
            "completed_tasks": state["completed_tasks"],
            "completion_count": state["completion_count"],
            "session_attempt_count": state["session_attempt_count"],
            "update_count": state["update_count"],
            "core_job_count": state["core_job_count"],
            "core_artifact_count": state["core_artifact_count"],
            "context_resolution_count": state["context_resolution_count"],
            "context_binding_violation_count": state["context_binding_violation_count"],
            "resume_allowed": state["resume_allowed"],
            "memory_aggregate": state["memory_aggregate"],
            "finding_codes": [],
        }
        _completed_stream_counts(result_like)
        loader, manifest, _paired = _verify_static_inputs(template, config_path)
        episodes = _build_episodes(loader, scope=template.scope)
        contract_receipts[template.scope] = _verify_meeting722_run_contract(
            output_directory=output,
            config=runtime,
            state=state,
            episodes=episodes,
        )
        memory_bytes, public_findings = _stream_public_evidence(
            output,
            expected_arm="online",
            state=state,
            expected_task_count=_EXPECTED_TASKS_PER_STREAM,
        )
        if public_findings or len(memory_bytes) != _EXPECTED_UPDATES_PER_STREAM:
            raise TaskwiseOnlineOnlyError("ONLINE_ONLY_PUBLIC_EVIDENCE_INVALID")
        events = _read_canonical_public_events(output / "public" / "events.jsonl")
        updates = tuple(row for row in events if row.get("kind") == "core_update")
        completions = tuple(row for row in events if row.get("kind") == "completion")
        if (
            len(updates) != _EXPECTED_UPDATES_PER_STREAM
            or len(completions) != _EXPECTED_COMPLETIONS_PER_STREAM
            or completions[0].get("memory") is not None
        ):
            raise TaskwiseOnlineOnlyError("ONLINE_ONLY_PUBLIC_EVIDENCE_INVALID")
        receipt_count, receipt_digest = _verify_stream_executor_success_evidence(
            package_root=PACKAGE_ROOT,
            config=runtime,
            completions=completions,
        )
        executor_receipt_count += receipt_count
        executor_receipt_digests[template.scope] = receipt_digest
        stream_artifacts = {
            row.get("artifact_id") for row in updates if type(row.get("artifact_id")) is str
        }
        stream_jobs = {
            row.get("core_job_id") for row in updates if type(row.get("core_job_id")) is str
        }
        if (
            len(stream_artifacts) != _EXPECTED_UPDATES_PER_STREAM
            or len(stream_jobs) != _EXPECTED_UPDATES_PER_STREAM
            or all_artifact_ids.intersection(stream_artifacts)
            or all_job_ids.intersection(stream_jobs)
        ):
            raise TaskwiseOnlineOnlyError("ONLINE_ONLY_STREAM_NAMESPACE_VIOLATION")
        all_artifact_ids.update(stream_artifacts)
        all_job_ids.update(stream_jobs)
        metrics = tuple(
            TaskwiseMemoryPublicMetricsV1.from_dict(row.get("memory_metrics")) for row in updates
        )
        private_rows = load_private_taskwise_results_v1(output)
        grouped = _group_online_rounds(tuple(private_rows))
        stream_uids = {rounds[0].task_uid for rounds in grouped.values()}
        if all_task_uids.intersection(stream_uids):
            raise TaskwiseOnlineOnlyError("ONLINE_ONLY_STREAM_NAMESPACE_VIOLATION")
        all_task_uids.update(stream_uids)
        all_grouped.append(grouped)
        manifest_hashes[template.scope] = manifest.public_sha256
        dataset_hashes.add(loader.manifest.combined_sha256)
        per_stream.append(
            {
                "stream_id": template.scope,
                "task_count": _EXPECTED_TASKS_PER_STREAM,
                "session_count": _EXPECTED_COMPLETIONS_PER_STREAM,
                "update_count": _EXPECTED_UPDATES_PER_STREAM,
                "core_job_count": _EXPECTED_UPDATES_PER_STREAM,
                "core_artifact_count": _EXPECTED_UPDATES_PER_STREAM,
                "context_resolution_count": _EXPECTED_UPDATES_PER_STREAM,
                "executor_success_receipt_count": receipt_count,
                "executor_success_receipts_sha256": receipt_digest,
                "round_0_accuracy": _round_accuracy(grouped, 0),
                "round_1_accuracy": _round_accuracy(grouped, 1),
                "round_2_accuracy": _round_accuracy(grouped, 2),
                "wrong_to_correct_0_to_1": sum(
                    (not row[0].correct) and row[1].correct for row in grouped.values()
                ),
                "wrong_to_correct_1_to_2": sum(
                    (not row[1].correct) and row[2].correct for row in grouped.values()
                ),
                "wrong_to_correct_0_to_2": sum(
                    (not row[0].correct) and row[2].correct for row in grouped.values()
                ),
                "correct_to_wrong_0_to_1": sum(
                    row[0].correct and (not row[1].correct) for row in grouped.values()
                ),
                "correct_to_wrong_1_to_2": sum(
                    row[1].correct and (not row[2].correct) for row in grouped.values()
                ),
                "memory_growth": {
                    "first_utf8_bytes": metrics[0].utf8_byte_count,
                    "last_utf8_bytes": metrics[-1].utf8_byte_count,
                    "max_utf8_bytes": max(item.utf8_byte_count for item in metrics),
                    "first_estimated_tokens": metrics[0].estimated_token_count,
                    "last_estimated_tokens": metrics[-1].estimated_token_count,
                    "max_estimated_tokens": max(item.estimated_token_count for item in metrics),
                    "max_section_items": max(item.max_section_items for item in metrics),
                },
                "category_metrics": _category_metrics(grouped),
                "generation_zero_reset_verified": True,
                "meeting722_contract_receipt_sha256": contract_receipts[template.scope],
            }
        )
    if (
        len(all_grouped) != _EXPECTED_STREAM_COUNT
        or len(all_task_uids) != _EXPECTED_SUITE_COUNTS["completed_tasks"]
        or len(all_artifact_ids) != _EXPECTED_SUITE_COUNTS["core_artifact_count"]
        or len(all_job_ids) != _EXPECTED_SUITE_COUNTS["core_job_count"]
        or len(dataset_hashes) != 1
    ):
        raise TaskwiseOnlineOnlyError("ONLINE_ONLY_SUITE_TOTAL_MISMATCH")

    all_tasks = [rounds for grouped in all_grouped for rounds in grouped.values()]
    task_position = [
        {
            "stream_position": position,
            "round_0_accuracy": (
                sum(grouped[position][0].correct for grouped in all_grouped)
                / _EXPECTED_STREAM_COUNT
            ),
        }
        for position in range(_EXPECTED_TASKS_PER_STREAM)
    ]
    all_categories: dict[str, dict[str, object]] = {}
    for category in CHEMBENCH4K_CATEGORIES:
        rows = [rounds for rounds in all_tasks if rounds[0].category == category]
        if not rows:
            raise TaskwiseOnlineOnlyError("ONLINE_ONLY_PRIVATE_EVIDENCE_INVALID")
        all_categories[category] = {
            "task_count": len(rows),
            "round_0_accuracy": sum(item[0].correct for item in rows) / len(rows),
            "round_1_accuracy": sum(item[1].correct for item in rows) / len(rows),
            "round_2_accuracy": sum(item[2].correct for item in rows) / len(rows),
        }
    report: dict[str, object] = {
        "schema_version": ONLINE_ONLY_REPORT_SCHEMA_V1,
        "labels": list(ONLINE_ONLY_REPORT_LABELS),
        "protocol_id": "taskwise_online_evolution_v1",
        "run_id": run_id,
        "source_commit": source_commit,
        "dataset_sha256": next(iter(dataset_hashes)),
        "per_stream_public_manifest_sha256": manifest_hashes,
        "standard_chembench4k_score_claimed": False,
        "causal_comparison_claimed": False,
        "stream_count": _EXPECTED_STREAM_COUNT,
        "task_count": _EXPECTED_SUITE_COUNTS["completed_tasks"],
        "session_count": _EXPECTED_SUITE_COUNTS["completion_count"],
        "update_count": _EXPECTED_SUITE_COUNTS["update_count"],
        "core_job_count": _EXPECTED_SUITE_COUNTS["core_job_count"],
        "core_artifact_count": _EXPECTED_SUITE_COUNTS["core_artifact_count"],
        "context_resolution_count": _EXPECTED_SUITE_COUNTS["context_resolution_count"],
        "executor_success_receipt_count": executor_receipt_count,
        "per_stream_executor_success_receipts_sha256": executor_receipt_digests,
        "executor_success_receipts_sha256": _sha256_payload(executor_receipt_digests),
        "round_0_accuracy": sum(row[0].correct for row in all_tasks) / len(all_tasks),
        "round_1_accuracy": sum(row[1].correct for row in all_tasks) / len(all_tasks),
        "round_2_accuracy": sum(row[2].correct for row in all_tasks) / len(all_tasks),
        "wrong_to_correct_0_to_1": sum(
            (not row[0].correct) and row[1].correct for row in all_tasks
        ),
        "wrong_to_correct_1_to_2": sum(
            (not row[1].correct) and row[2].correct for row in all_tasks
        ),
        "wrong_to_correct_0_to_2": sum(
            (not row[0].correct) and row[2].correct for row in all_tasks
        ),
        "correct_to_wrong_0_to_1": sum(
            row[0].correct and (not row[1].correct) for row in all_tasks
        ),
        "correct_to_wrong_1_to_2": sum(
            row[1].correct and (not row[2].correct) for row in all_tasks
        ),
        "per_category": all_categories,
        "per_stream": per_stream,
        "task_position_round_0_trend": task_position,
        "infrastructure_failures": suite_state["infrastructure_failures"],
        "security_violations": suite_state["security_violations"],
        "context_binding_violations": suite_state["context_binding_violations"],
        "artifact_validation_failures": suite_state["artifact_validation_failures"],
        "cleanup_residual_count": 0,
    }
    forbidden_keys = {
        "absolute_delta",
        "decision",
        "go_gate",
        "paired_final_round",
        "p_value",
        "confidence_interval",
    }
    if forbidden_keys.intersection(report):
        raise TaskwiseOnlineOnlyError("ONLINE_ONLY_REPORT_CLASSIFICATION_VIOLATION")
    return report


def run_online_only_pilot500_v1(
    *,
    run_id: str,
    repository_root: Path = REPOSITORY_ROOT,
    stream_runner: StreamRunnerV1 | None = None,
) -> dict[str, object]:
    """Run ten independent online streams in an explicitly unpaired namespace."""

    validated_run_id = validate_online_only_run_id_v1(run_id)
    authorization = require_online_canary_authorization_v1(PACKAGE_ROOT)
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
        "classification": list(ONLINE_ONLY_REPORT_LABELS),
        "primary_classification": ONLINE_ONLY_CLASSIFICATION,
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
        "completed_tasks": 0,
        "completion_count": 0,
        "session_attempt_count": 0,
        "update_count": 0,
        "core_job_count": 0,
        "core_artifact_count": 0,
        "context_resolution_count": 0,
        "infrastructure_failures": 0,
        "security_violations": 0,
        "evolution_update_failures": 0,
        "artifact_validation_failures": 0,
        "context_binding_violations": 0,
        "executor_failures": 0,
        "executor_failure_codes": {},
        "source_commit": source_commit,
        "online_canary_generation_id": authorization.online_canary_generation_id,
        "canary_receipt_sha256": authorization.receipt_sha256,
        "online_pilot_binding_sha256": authorization.online_pilot_binding_sha256,
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
        runtime_config: TaskwiseExperimentConfigV1 | None = None
        output_directory: Path | None = None
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
        _update_online_only_suite_totals(state)
        persist()
        try:
            current_authorization = _require_current_authorization(authorization)
            runtime_config = online_only_runtime_config_v1(
                template,
                run_id=validated_run_id,
            )
            output_directory = _resolve_workspace_path(
                runtime_config.output_directory,
                field_name="output_directory",
                must_exist=False,
            )
            result = runner(
                config_path,
                runtime_config,
                validated_run_id,
                current_authorization,
            )
            evidence = _online_only_stream_result_evidence(result)
        except BaseException as exc:
            evidence = _failure_evidence(output_directory, exc)
            state["streams"][scope] = {
                "action": "ORCHESTRATION_ERROR",
                **evidence,
            }
            state["active_scope"] = None
            state["status"] = "FAILED"
            _update_online_only_suite_totals(state)
            persist()
            raise
        state["streams"][scope] = {"action": "STARTED", **evidence}
        if not evidence["completed"]:
            state["active_scope"] = None
            state["status"] = "FAILED"
            _update_online_only_suite_totals(state)
            persist()
            raise TaskwiseOnlineOnlyError("ONLINE_ONLY_SUITE_TERMINAL_FAILURE")
        state["streams"][scope]["action"] = "COMPLETED"
        state["active_scope"] = None
        _update_online_only_suite_totals(state)
        persist()
    try:
        _verify_completed_suite_totals(state)
        report = _build_online_only_descriptive_report(
            run_id=validated_run_id,
            source_commit=source_commit,
            preflight=preflight,
            suite_state=state,
        )
        report_path = root / "online_only_descriptive_report.json"
        _create_file_exclusive(report_path, report, mode=0o644)
        state["descriptive_report_sha256"] = hashlib.sha256(_canonical_bytes(report)).hexdigest()
        state["descriptive_report_path"] = report_path.name
    except BaseException:
        state["status"] = "FAILED"
        state["active_scope"] = None
        _update_online_only_suite_totals(state)
        persist()
        raise
    state["status"] = "COMPLETED"
    _update_online_only_suite_totals(state)
    persist()
    return state


def recover_completed_online_only_report_v1(
    *,
    run_id: str,
    repository_root: Path = REPOSITORY_ROOT,
) -> dict[str, object]:
    """Recover reporting only from immutable, fully completed stream evidence."""

    validated_run_id = validate_online_only_run_id_v1(run_id)
    run_root = online_only_run_root_v1(repository_root, validated_run_id)
    state_path = run_root / "online_only_suite_state.json"
    try:
        state_raw = state_path.read_bytes()
        state = json.loads(state_raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TaskwiseOnlineOnlyError("ONLINE_ONLY_RECOVERY_STATE_INVALID") from exc
    streams = state.get("streams") if type(state) is dict else None
    if (
        type(state) is not dict
        or state_raw != _canonical_bytes(state)
        or state.get("schema_version") != ONLINE_ONLY_SCHEMA_V1
        or state.get("run_id") != validated_run_id
        or state.get("status") != "FAILED"
        or state.get("active_scope") is not None
        or type(streams) is not dict
        or set(streams) != set(PILOT500_STREAM_SCOPES)
        or any(
            type(entry) is not dict
            or entry.get("completed") is not True
            or entry.get("run_status") != "COMPLETED"
            or entry.get("failure_code") is not None
            for entry in streams.values()
        )
    ):
        raise TaskwiseOnlineOnlyError("ONLINE_ONLY_RECOVERY_STATE_INVALID")
    _verify_completed_suite_totals(state)

    preflight: list[tuple[Path, TaskwiseExperimentConfigV1]] = []
    reporter_commits: set[str] = set()
    for scope in PILOT500_STREAM_SCOPES:
        config_path = CONFIG_ROOT / f"online_{scope}_taskwise_online_v1.yaml"
        template = load_taskwise_config_v1(config_path)
        reporter_commits.add(verify_taskwise_source_gate_v1(template))
        _verify_static_inputs(template, config_path)
        preflight.append((config_path, template))
    if len(reporter_commits) != 1:
        raise TaskwiseOnlineOnlyError("ONLINE_ONLY_RECOVERY_SOURCE_INVALID")
    reporter_source_commit = next(iter(reporter_commits))
    execution_source_commit = state.get("source_commit")
    if (
        type(execution_source_commit) is not str
        or re.fullmatch(r"[0-9a-f]{40}", execution_source_commit) is None
    ):
        raise TaskwiseOnlineOnlyError("ONLINE_ONLY_RECOVERY_SOURCE_INVALID")

    report = _build_online_only_descriptive_report(
        run_id=validated_run_id,
        source_commit=execution_source_commit,
        preflight=preflight,
        suite_state=state,
    )
    state_sha256 = hashlib.sha256(state_raw).hexdigest()
    report["report_recovery"] = {
        "classification": "POSTHOC_REPORT_ONLY_FROM_IMMUTABLE_COMPLETED_EXECUTION_EVIDENCE",
        "original_suite_status": "FAILED",
        "original_failure_phase": "REPORT_FINALIZATION",
        "execution_evidence_status": "COMPLETED",
        "execution_source_commit": execution_source_commit,
        "reporter_source_commit": reporter_source_commit,
        "original_suite_state_sha256": state_sha256,
        "model_calls_for_recovery": 0,
        "original_run_mutated": False,
    }
    report_sha256 = hashlib.sha256(_canonical_bytes(report)).hexdigest()
    receipt = {
        "schema_version": ONLINE_ONLY_RECOVERY_RECEIPT_SCHEMA_V1,
        "status": "PASS",
        "classification": list(ONLINE_ONLY_REPORT_LABELS),
        "run_id": validated_run_id,
        "execution_source_commit": execution_source_commit,
        "reporter_source_commit": reporter_source_commit,
        "original_suite_state_sha256": state_sha256,
        "recovered_report_sha256": report_sha256,
        "completed_streams": state["completed_streams"],
        "completed_tasks": state["completed_tasks"],
        "completion_count": state["completion_count"],
        "update_count": state["update_count"],
        "core_artifact_count": state["core_artifact_count"],
        "context_resolution_count": state["context_resolution_count"],
        "security_violations": state["security_violations"],
        "context_binding_violations": state["context_binding_violations"],
        "artifact_validation_failures": state["artifact_validation_failures"],
        "model_calls_for_recovery": 0,
        "original_run_mutated": False,
    }
    output_root = online_only_recovered_report_root_v1(
        repository_root,
        run_id=validated_run_id,
        reporter_source_commit=reporter_source_commit,
    )
    try:
        output_root.mkdir(parents=True, mode=0o700)
    except FileExistsError as exc:
        raise TaskwiseOnlineOnlyError("ONLINE_ONLY_RECOVERED_REPORT_EXISTS") from exc
    output_root.chmod(0o700)
    _create_file_exclusive(
        output_root / "online_only_descriptive_report.json",
        report,
        mode=0o644,
    )
    _create_file_exclusive(
        output_root / "report_recovery_receipt.json",
        receipt,
        mode=0o644,
    )
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chembench-taskwise-online-only-v1")
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run-pilot500")
    run_parser.add_argument("--run-id", required=True)
    recover_parser = commands.add_parser("recover-report")
    recover_parser.add_argument("--run-id", required=True)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "run-pilot500":
            payload = run_online_only_pilot500_v1(run_id=arguments.run_id)
        else:
            payload = recover_completed_online_only_report_v1(run_id=arguments.run_id)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        failure = {
            "status": "BLOCKED",
            "classification": list(ONLINE_ONLY_REPORT_LABELS),
            "primary_classification": ONLINE_ONLY_CLASSIFICATION,
            "error_type": type(exc).__name__,
        }
        failure.update(_public_cli_failure_counts(REPOSITORY_ROOT, arguments.run_id))
        print(
            json.dumps(failure, sort_keys=True),
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
    "online_only_recovered_report_root_v1",
    "online_only_run_root_v1",
    "online_only_runtime_config_v1",
    "recover_completed_online_only_report_v1",
    "run_online_only_pilot500_v1",
    "validate_online_only_run_id_v1",
]
