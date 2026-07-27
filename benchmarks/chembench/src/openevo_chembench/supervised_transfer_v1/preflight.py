"""Identity-closed, aggregate-safe authority for reusable paid preflight stages."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from openevo.evolution.framework import load_verified_framework_registry

from openevo_chembench.chembench4k_models import CHEMBENCH4K_CATEGORIES
from openevo_chembench.local_codex_executor import _executor_policy_sha256
from openevo_chembench.supervised_transfer_v1.common import (
    canonical_json_bytes,
    canonical_pretty_json_bytes,
    sha256_bytes,
    write_public_file,
)
from openevo_chembench.supervised_transfer_v1.config import (
    EVOLUTION_TARGET_METHODS,
    PREFLIGHT_CORE_ARTIFACTS,
    PREFLIGHT_CORE_JOBS,
)
from openevo_chembench.taskwise_config_v1 import load_taskwise_config_v1

if TYPE_CHECKING:
    from openevo_chembench.supervised_transfer_v1.experiment import ExperimentInputsV1


PREFLIGHT_AUTHORITY_SCHEMA = "SupervisedTransferPreflightAuthorityV1"
PREFLIGHT_AUTHORITY_FILENAME = "preflight_authority_v1.json"
_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}\Z", re.ASCII)
_EXPECTED_TASK_COUNTS = {
    ("SUPERVISED_UPDATE_SMOKE", "online_smoke"): 1,
    ("ONLINE_CANARY", "online_canary"): 27,
    ("CONTROL_CANARY", "control_canary"): 27,
    ("PROBE_SMOKE", "control_probe_smoke"): 9,
    ("PROBE_SMOKE", "online_probe_smoke"): 9,
    ("PROBE_CHECKPOINT_00", "control_probe"): 90,
    ("PROBE_CHECKPOINT_00", "online_probe"): 90,
}
_EXPECTED_CORE_COUNTS = {
    "SUPERVISED_UPDATE_SMOKE": 1,
    "ONLINE_CANARY": 18,
}


class PreflightAuthorityError(RuntimeError):
    """Closed preflight evidence or identity failure."""

    def __init__(self, finding_code: str) -> None:
        self.finding_code = finding_code
        super().__init__(finding_code)


def write_preflight_authority_v1(
    *,
    inputs: ExperimentInputsV1,
    source_run_id: str,
    issued_at_utc: str,
) -> tuple[dict[str, object], str]:
    evidence = inspect_preflight_evidence_v1(inputs=inputs, source_run_id=source_run_id)
    receipt = {
        "schema_version": PREFLIGHT_AUTHORITY_SCHEMA,
        **evidence,
        "issued_at_utc": issued_at_utc,
    }
    path = (
        inputs.repository_root
        / inputs.config.result_root
        / "runs"
        / source_run_id
        / "public"
        / PREFLIGHT_AUTHORITY_FILENAME
    )
    if path.exists():
        raise PreflightAuthorityError("PREFLIGHT_AUTHORITY_ALREADY_EXISTS")
    write_public_file(path, canonical_pretty_json_bytes(receipt))
    return receipt, sha256_bytes(path.read_bytes())


def verify_preflight_authority_v1(
    *,
    inputs: ExperimentInputsV1,
    source_run_id: str,
) -> tuple[dict[str, object], str]:
    _require_run_id(source_run_id)
    result_root = (
        inputs.repository_root / inputs.config.result_root / "runs" / source_run_id
    )
    state_path = result_root / "public/run_state.json"
    receipt_path = result_root / "public" / PREFLIGHT_AUTHORITY_FILENAME
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PreflightAuthorityError("PREFLIGHT_AUTHORITY_UNAVAILABLE") from exc
    if (
        not isinstance(state, dict)
        or state.get("status") != "PREFLIGHT_COMPLETED"
        or state.get("stage") != "PREFLIGHT_COMPLETED"
        or state.get("run_mode") != "preflight"
        or state.get("preflight_authority_sha256")
        != sha256_bytes(receipt_path.read_bytes())
    ):
        raise PreflightAuthorityError("PREFLIGHT_AUTHORITY_SOURCE_NOT_TERMINAL")
    if not isinstance(receipt, dict) or receipt.get("schema_version") != (
        PREFLIGHT_AUTHORITY_SCHEMA
    ):
        raise PreflightAuthorityError("PREFLIGHT_AUTHORITY_SCHEMA_INVALID")
    issued = receipt.get("issued_at_utc")
    if type(issued) is not str or not issued:
        raise PreflightAuthorityError("PREFLIGHT_AUTHORITY_SCHEMA_INVALID")
    expected = {
        "schema_version": PREFLIGHT_AUTHORITY_SCHEMA,
        **inspect_preflight_evidence_v1(inputs=inputs, source_run_id=source_run_id),
        "issued_at_utc": issued,
    }
    if receipt != expected:
        raise PreflightAuthorityError("PREFLIGHT_AUTHORITY_IDENTITY_MISMATCH")
    return receipt, sha256_bytes(receipt_path.read_bytes())


def inspect_preflight_evidence_v1(
    *,
    inputs: ExperimentInputsV1,
    source_run_id: str,
) -> dict[str, object]:
    """Recompute authority fields from immutable run evidence, never booleans."""

    _require_run_id(source_run_id)
    repository = inputs.repository_root
    result_root = repository / inputs.config.result_root / "runs" / source_run_id
    state_root = repository / inputs.config.state_root / "runs" / source_run_id
    public_path = result_root / "public/events.jsonl"
    private_path = state_root / "private/events.jsonl"
    state_path = result_root / "public/run_state.json"
    checkpoint_path = result_root / "public/checkpoints/checkpoint_00.json"
    public = _read_jsonl(public_path)
    private = _read_jsonl(private_path)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PreflightAuthorityError("PREFLIGHT_EVIDENCE_UNAVAILABLE") from exc

    task_rows = [row for row in public if row.get("kind") == "TASK_MODEL_EXECUTED"]
    core_rows = [row for row in public if row.get("kind") == "REFLECTOR_SUPERVISED"]
    if len(task_rows) != 253 or len(core_rows) != 19 or len(public) != 272:
        raise PreflightAuthorityError("PREFLIGHT_PUBLIC_CARDINALITY_INVALID")
    observed_task_counts = Counter(
        (str(row.get("stage")), str(row.get("logical_arm"))) for row in task_rows
    )
    if dict(observed_task_counts) != _EXPECTED_TASK_COUNTS:
        raise PreflightAuthorityError("PREFLIGHT_STAGE_CARDINALITY_INVALID")
    observed_core_counts = Counter(str(row.get("stage")) for row in core_rows)
    if dict(observed_core_counts) != _EXPECTED_CORE_COUNTS:
        raise PreflightAuthorityError("PREFLIGHT_CORE_CARDINALITY_INVALID")
    if any(not _valid_multitarget_core_row(row) for row in core_rows):
        raise PreflightAuthorityError("PREFLIGHT_CORE_TARGET_SET_INVALID")

    train_uids = {task.uid for task in inputs.train}
    probe_uids = {task.uid for task in inputs.probe}
    if any(row.get("task_uid") not in train_uids for row in core_rows):
        raise PreflightAuthorityError("PREFLIGHT_REFLECTOR_SCOPE_INVALID")
    context_fields = (
        "memory_artifact_id",
        "skill_artifact_id",
        "agent_system_artifact_id",
    )
    for row in task_rows:
        values = tuple(row.get(field) for field in context_fields)
        expects_context = (
            row.get("logical_arm") == "online_canary"
            and row.get("round_index") in (1, 2)
        )
        if expects_context and any(value is None for value in values):
            raise PreflightAuthorityError("PREFLIGHT_ONLINE_CONTEXT_INVALID")
        if not expects_context and any(value is not None for value in values):
            raise PreflightAuthorityError("PREFLIGHT_CONTEXT_SCOPE_INVALID")
    checkpoint_probe = [
        row for row in task_rows if row.get("stage") == "PROBE_CHECKPOINT_00"
    ]
    if {row.get("task_uid") for row in checkpoint_probe} != probe_uids:
        raise PreflightAuthorityError("PREFLIGHT_PROBE_UID_SET_INVALID")
    if any(str(row.get("stage")) == "FINAL_TEST" for row in public):
        raise PreflightAuthorityError("PREFLIGHT_TEST_ACCESS_INVALID")

    attempt_rows = [row for row in private if row.get("kind") == "TASK_MODEL_ATTEMPTED"]
    evaluation_rows = [row for row in private if row.get("kind") == "PRIVATE_EVALUATED"]
    if len(attempt_rows) != 253 or len(evaluation_rows) != 253 or len(private) != 506:
        raise PreflightAuthorityError("PREFLIGHT_PRIVATE_CARDINALITY_INVALID")
    public_sessions = {str(row.get("session_id")) for row in task_rows}
    if (
        len(public_sessions) != 253
        or {str(row.get("session_id")) for row in attempt_rows} != public_sessions
        or {str(row.get("session_id")) for row in evaluation_rows} != public_sessions
    ):
        raise PreflightAuthorityError("PREFLIGHT_SESSION_BINDING_INVALID")

    success_rows, success_inventory = _load_success_receipts(state_root)
    if len(success_rows) != 253 or {
        str(row.get("session_id")) for row in success_rows
    } != public_sessions:
        raise PreflightAuthorityError("PREFLIGHT_EXECUTOR_RECEIPT_INVALID")
    identity = _effective_identity(inputs)
    expected_policy = identity["executor_policy_sha256"]
    if any(
        row.get("run_id") != source_run_id
        or row.get("status") != "COMPLETED"
        or row.get("completion_observed") is not True
        or row.get("process_return_code") != 0
        or row.get("process_signal") is not None
        or row.get("cleanup_status") != "COMPLETE"
        or row.get("residual_root_count") != 0
        or row.get("tool_event_count") != 0
        or row.get("model") != inputs.config.model
        or row.get("codex_cli_version") != inputs.codex_cli_version.removeprefix(
            "codex-cli "
        )
        or row.get("codex_executable_sha256")
        != inputs.managed_codex.executable_sha256
        or row.get("executor_policy_sha256") != expected_policy
        for row in success_rows
    ):
        raise PreflightAuthorityError("PREFLIGHT_EXECUTOR_RECEIPT_INVALID")
    diagnostic_count = sum(
        1
        for path in (state_root / "private/executor").glob("*/*.json")
        if json.loads(path.read_text(encoding="utf-8")).get("schema_version")
        == "taskwise_executor_private_diagnostic_v1"
    )
    reflector_receipts, reflector_inventory = _load_reflector_receipts(state_root)
    if diagnostic_count or len(reflector_receipts) != 19 or any(
        row.get("status") != "COMPLETED"
        or row.get("cleanup_complete") is not True
        or row.get("event_counts") not in ({}, [])
        or row.get("retry_allowed") is not False
        or row.get("resume_allowed") is not False
        or row.get("replacement_completion_allowed") is not False
        or row.get("real_codex_sha256")
        != inputs.managed_codex.executable_sha256
        for row in reflector_receipts
    ):
        raise PreflightAuthorityError("PREFLIGHT_CLEANUP_OR_REFLECTOR_INVALID")

    if (
        not isinstance(state, dict)
        or state.get("source_commit") != inputs.source_commit
        or state.get("run_mode") != "preflight"
        or state.get("split_receipt_sha256") != inputs.split_receipt_sha256
        or state.get("codex_runtime_source") != inputs.managed_codex.source
        or state.get("codex_runtime_identity_sha256") != inputs.managed_codex.digest
        or state.get("codex_executable_sha256")
        != inputs.managed_codex.executable_sha256
        or state.get("task_sessions") != 253
        or state.get("reflector_completions") != 19
        or state.get("core_jobs") != PREFLIGHT_CORE_JOBS
        or state.get("core_artifacts") != PREFLIGHT_CORE_ARTIFACTS
        or any(
            state.get(name) != 0
            for name in (
                "security_findings",
                "context_findings",
                "artifact_findings",
                "infrastructure_failures",
            )
        )
    ):
        raise PreflightAuthorityError("PREFLIGHT_RUN_STATE_INVALID")
    artifact_ids = checkpoint.get("artifact_ids") if isinstance(checkpoint, dict) else None
    payloads = (
        checkpoint.get("artifact_payload_sha256") if isinstance(checkpoint, dict) else None
    )
    contexts = (
        checkpoint.get("context_resolution_sha256")
        if isinstance(checkpoint, dict)
        else None
    )
    if (
        not isinstance(checkpoint, dict)
        or checkpoint.get("checkpoint") != 0
        or checkpoint.get("train_items_per_category") != 0
        or checkpoint.get("probe_feedback_used") is not False
        or not isinstance(artifact_ids, dict)
        or not isinstance(payloads, dict)
        or not isinstance(contexts, dict)
        or set(artifact_ids) != set(CHEMBENCH4K_CATEGORIES)
        or set(payloads) != set(CHEMBENCH4K_CATEGORIES)
        or set(contexts) != set(CHEMBENCH4K_CATEGORIES)
        or checkpoint.get("target_order")
        != ["text_memory", "skill_bundle", "agent_system"]
        or any(not _empty_checkpoint_targets(value) for value in artifact_ids.values())
        or any(not _empty_checkpoint_targets(value) for value in payloads.values())
        or any(not _empty_checkpoint_targets(value) for value in contexts.values())
    ):
        raise PreflightAuthorityError("PREFLIGHT_CHECKPOINT_ZERO_INVALID")

    probe_rows = _aggregate_checkpoint_zero(evaluation_rows)
    return {
        "protocol_id": inputs.config.protocol_id,
        "source_run_id": source_run_id,
        **identity,
        "public_events_sha256": sha256_bytes(public_path.read_bytes()),
        "private_events_sha256": sha256_bytes(private_path.read_bytes()),
        "checkpoint_zero_receipt_sha256": sha256_bytes(checkpoint_path.read_bytes()),
        "executor_success_receipt_inventory_sha256": success_inventory,
        "reflector_receipt_inventory_sha256": reflector_inventory,
        "stage_task_counts": {
            f"{stage}:{arm}": count
            for (stage, arm), count in sorted(_EXPECTED_TASK_COUNTS.items())
        },
        "stage_reflector_counts": dict(sorted(_EXPECTED_CORE_COUNTS.items())),
        "task_sessions": 253,
        "reflector_completions": 19,
        "core_jobs": PREFLIGHT_CORE_JOBS,
        "core_artifacts": PREFLIGHT_CORE_ARTIFACTS,
        "evolution_targets": dict(EVOLUTION_TARGET_METHODS),
        "security_findings": 0,
        "context_findings": 0,
        "artifact_findings": 0,
        "infrastructure_failures": 0,
        "cleanup_residuals": 0,
        "probe_evolution_jobs": 0,
        "test_model_calls": 0,
        "checkpoint_zero_aggregate_rows": probe_rows,
    }


def _valid_multitarget_core_row(row: dict[str, Any]) -> bool:
    auxiliary = row.get("auxiliary_targets")
    if not isinstance(auxiliary, list) or len(auxiliary) != 2:
        return False
    expected = dict(EVOLUTION_TARGET_METHODS)
    if row.get("method_id") != expected["text_memory"]:
        return False
    for item, target in zip(auxiliary, ("skill_bundle", "agent_system"), strict=True):
        if not isinstance(item, dict):
            return False
        inspection = item.get("inspection")
        if (
            item.get("target_id") != target
            or item.get("method_id") != expected[target]
            or not item.get("job_id")
            or not item.get("artifact_id")
            or not isinstance(inspection, dict)
            or inspection.get("target_id") != target
            or inspection.get("finding_codes") != []
        ):
            return False
    return True


def _empty_checkpoint_targets(value: object) -> bool:
    return isinstance(value, dict) and value == {
        "text_memory": None,
        "skill_bundle": None,
        "agent_system": None,
    }


def _effective_identity(inputs: ExperimentInputsV1) -> dict[str, object]:
    repository = inputs.repository_root
    config_path = (
        repository
        / "benchmarks/chembench/configs/chembench_supervised_transfer_v1.yaml"
    )
    policies = []
    for name in (
        "control_canary9_taskwise_online_v1.yaml",
        "online_canary9_taskwise_online_v1.yaml",
    ):
        config = replace(
            load_taskwise_config_v1(repository / "benchmarks/chembench/configs" / name),
            codex_cli_version=inputs.codex_cli_version.removeprefix("codex-cli "),
        )
        policies.append(_executor_policy_sha256(config))
    if len(set(policies)) != 1:
        raise PreflightAuthorityError("PREFLIGHT_EXECUTOR_POLICY_DIVERGED")
    framework_lock = repository / inputs.config.framework_lock
    registry = load_verified_framework_registry(framework_lock)
    target_methods = dict(EVOLUTION_TARGET_METHODS)
    manifests = inputs.manifest_root
    return {
        "source_commit": inputs.source_commit,
        "supervised_config_sha256": sha256_bytes(config_path.read_bytes()),
        "effective_config_sha256": inputs.config.digest,
        "dataset_revision": inputs.loader.manifest.revision,
        "dataset_combined_sha256": inputs.loader.manifest.combined_sha256,
        "split_receipt_sha256": inputs.split_receipt_sha256,
        "train_private_manifest_sha256": sha256_bytes(
            (manifests / "train_private_manifest.jsonl").read_bytes()
        ),
        "probe_private_manifest_sha256": sha256_bytes(
            (manifests / "probe_private_manifest.jsonl").read_bytes()
        ),
        "primary_test_private_manifest_sha256": sha256_bytes(
            (manifests / "test_primary_private_manifest.jsonl").read_bytes()
        ),
        "recovery_test_private_manifest_sha256": sha256_bytes(
            (manifests / "test_recovery_01_private_manifest.jsonl").read_bytes()
        ),
        "model": inputs.config.model,
        "reasoning_effort": inputs.config.reasoning_effort,
        "codex_cli_version": inputs.codex_cli_version,
        "codex_runtime_source": inputs.managed_codex.source,
        "codex_runtime_identity_sha256": inputs.managed_codex.digest,
        "codex_executable_sha256": inputs.managed_codex.executable_sha256,
        "codex_managed_receipt_sha256": inputs.managed_codex.receipt_sha256,
        "executor_policy_sha256": policies[0],
        "framework_lock_sha256": sha256_bytes(framework_lock.read_bytes()),
        "verified_registry_digest": registry.snapshot.registry_digest,
        "evolution_targets": target_methods,
        "evolution_method_identity_sha256": {
            target: registry.snapshot.identity_digest_for("method", method)
            for target, method in EVOLUTION_TARGET_METHODS
        },
    }


def _aggregate_checkpoint_zero(rows: list[dict[str, Any]]) -> list[dict[str, object]]:
    selected = [
        row
        for row in rows
        if row.get("stage") == "PROBE_CHECKPOINT_00"
        and row.get("logical_arm") in {"control_probe", "online_probe"}
    ]
    result: list[dict[str, object]] = []
    for category in ("overall", *CHEMBENCH4K_CATEGORIES):
        subset = [
            row
            for row in selected
            if category == "overall" or row.get("category") == category
        ]
        control = [row for row in subset if row["logical_arm"] == "control_probe"]
        online = [row for row in subset if row["logical_arm"] == "online_probe"]
        right = {row["task_uid"]: row for row in online}
        if len(right) != len(online) or {row["task_uid"] for row in control} != set(right):
            raise PreflightAuthorityError("PREFLIGHT_PROBE_PAIRING_INVALID")
        pairs = [(row, right[row["task_uid"]]) for row in control]
        expected = 90 if category == "overall" else 10
        if len(pairs) != expected:
            raise PreflightAuthorityError("PREFLIGHT_PROBE_PAIRING_INVALID")
        control_accuracy = sum(bool(left["correct"]) for left, _ in pairs) / expected
        online_accuracy = sum(bool(right_row["correct"]) for _, right_row in pairs) / expected
        result.append(
            {
                "checkpoint": 0,
                "category": category,
                "n": expected,
                "control_accuracy": control_accuracy,
                "online_accuracy": online_accuracy,
                "delta": online_accuracy - control_accuracy,
                "wrong_to_correct": sum(
                    not left["correct"] and right_row["correct"]
                    for left, right_row in pairs
                ),
                "correct_to_wrong": sum(
                    left["correct"] and not right_row["correct"]
                    for left, right_row in pairs
                ),
                "control_strict_parse_rate": sum(
                    left.get("strict_parse_status") == "parsed" for left, _ in pairs
                )
                / expected,
                "online_strict_parse_rate": sum(
                    right_row.get("strict_parse_status") == "parsed"
                    for _, right_row in pairs
                )
                / expected,
            }
        )
    return result


def _load_success_receipts(state_root: Path) -> tuple[list[dict[str, Any]], str]:
    rows: list[dict[str, Any]] = []
    inventory: list[dict[str, object]] = []
    executor_root = state_root / "private/executor"
    for path in sorted(executor_root.glob("*/*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != "taskwise_executor_private_success_v2":
            continue
        rows.append(payload)
        raw = path.read_bytes()
        inventory.append(
            {
                "relative_path": path.relative_to(executor_root).as_posix(),
                "size": len(raw),
                "sha256": sha256_bytes(raw),
            }
        )
    return rows, sha256_bytes(canonical_json_bytes(inventory))


def _load_reflector_receipts(state_root: Path) -> tuple[list[dict[str, Any]], str]:
    rows: list[dict[str, Any]] = []
    inventory: list[dict[str, object]] = []
    core_root = state_root / "private/core_preflight"
    for path in sorted(core_root.glob("**/private_reflector_events/*/receipt.json")):
        raw = path.read_bytes()
        rows.append(json.loads(raw))
        inventory.append(
            {
                "relative_path": path.relative_to(core_root).as_posix(),
                "size": len(raw),
                "sha256": sha256_bytes(raw),
            }
        )
    return rows, sha256_bytes(canonical_json_bytes(inventory))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        values = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PreflightAuthorityError("PREFLIGHT_EVIDENCE_UNAVAILABLE") from exc
    if any(not isinstance(value, dict) for value in values):
        raise PreflightAuthorityError("PREFLIGHT_EVIDENCE_SCHEMA_INVALID")
    return values


def _require_run_id(run_id: str) -> None:
    if type(run_id) is not str or _RUN_ID_RE.fullmatch(run_id) is None:
        raise ValueError("source_run_id is invalid")


__all__ = [
    "PREFLIGHT_AUTHORITY_FILENAME",
    "PREFLIGHT_AUTHORITY_SCHEMA",
    "PreflightAuthorityError",
    "inspect_preflight_evidence_v1",
    "verify_preflight_authority_v1",
    "write_preflight_authority_v1",
]
