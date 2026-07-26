"""Strict, content-free compatibility audit for a completed Control500 run."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
import yaml

from openevo.evolution.framework import canonical_digest

from openevo_chembench.chembench4k_evaluation import official_first_capital_parser
from openevo_chembench.taskwise_online_runner_v1 import _chain_rows


_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_STREAM_COUNT = 10
_TASKS_PER_STREAM = 50
_SESSIONS_PER_TASK = 3
_SAFE_SOURCE_DIFF = frozenset(
    {
        "benchmarks/chembench/README.md",
        "benchmarks/chembench/TASKWISE_ONLINE_RUNBOOK_V1.md",
        "benchmarks/chembench/configs/control_canary9_taskwise_online_v1.yaml",
        "benchmarks/chembench/configs/online_canary9_taskwise_online_v1.yaml",
        "benchmarks/chembench/manifests/chembench_source_manifest_v2.json",
        "benchmarks/chembench/scripts/run_online_canary9_taskwise_online_v1.sh",
        "benchmarks/chembench/scripts/run_online_only_pilot500_streams_taskwise_online_v1.sh",
        "benchmarks/chembench/src/openevo_chembench/artifact_validator_v2.py",
        "benchmarks/chembench/src/openevo_chembench/reflector_execution_boundary_v2.py",
        "benchmarks/chembench/src/openevo_chembench/source_identity_v2.py",
        "benchmarks/chembench/src/openevo_chembench/taskwise_canary_receipt_v1.py",
        "benchmarks/chembench/src/openevo_chembench/taskwise_core_evolution_v1.py",
        "benchmarks/chembench/src/openevo_chembench/taskwise_online_canary_receipt_v1.py",
        "benchmarks/chembench/src/openevo_chembench/taskwise_online_only_v1.py",
    }
)
_CONFIG_SEMANTIC_FIELDS = (
    "protocol_id",
    "arm",
    "scope",
    "dataset",
    "model",
    "reasoning_effort",
    "codex_cli_version",
    "prompt_renderer_id",
    "parser_id",
    "evaluator_id",
    "attempts_per_task",
    "inter_round_slots",
    "evolution_updates_per_task",
    "carry_memory_across_tasks",
    "fixed_round_budget",
    "stop_when_correct",
    "final_score_round",
    "executor",
)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class ControlRunCompatibilityReceiptV1(_FrozenModel):
    """Twelve direct predicates required before reusing historical Control500."""

    schema_version: Literal["chembench4k_control_run_compatibility_receipt_v1"] = (
        "chembench4k_control_run_compatibility_receipt_v1"
    )
    status: Literal["COMPATIBLE", "INCOMPATIBLE"]
    historical_source_commit: str
    current_source_commit: str
    historical_generation_id_sha256: str
    historical_attempt_id_sha256: str
    control_task_manifest_identical: bool
    stream_order_identical: bool
    model_reasoning_codex_identical: bool
    prompt_parser_evaluator_identical: bool
    three_sessions_per_task_identical: bool
    executor_success_semantics_proven_identical: bool
    control_validator_absent: bool
    control_evolution_absent: bool
    predictions_replay_identical: bool
    historical_run_complete: bool
    result_digest_chain_closed: bool
    source_diff_allowlisted: bool
    completed_streams: int = Field(ge=0)
    completed_tasks: int = Field(ge=0)
    completed_sessions: int = Field(ge=0)
    success_receipt_count: int = Field(ge=0)
    infrastructure_failures: int = Field(ge=0)
    security_violations: int = Field(ge=0)
    context_binding_violations: int = Field(ge=0)
    contract_violations: int = Field(ge=0)
    changed_file_count: int = Field(ge=0)
    disallowed_changed_file_count: int = Field(ge=0)
    public_chain_set_sha256: str
    private_chain_set_sha256: str
    result_evidence_sha256: str
    source_diff_sha256: str
    allowlist_sha256: str
    finding_codes: tuple[str, ...]

    @field_validator("historical_source_commit", "current_source_commit")
    @classmethod
    def _commit(cls, value: str) -> str:
        if _COMMIT_RE.fullmatch(value) is None:
            raise ValueError("compatibility source commit is invalid")
        return value

    @field_validator(
        "historical_generation_id_sha256",
        "historical_attempt_id_sha256",
        "public_chain_set_sha256",
        "private_chain_set_sha256",
        "result_evidence_sha256",
        "source_diff_sha256",
        "allowlist_sha256",
    )
    @classmethod
    def _digest(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("compatibility digest must be SHA-256")
        return value

    @field_validator("finding_codes")
    @classmethod
    def _findings(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if values != tuple(sorted(set(values))) or any(
            re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", value) is None for value in values
        ):
            raise ValueError("compatibility findings are not a closed sorted set")
        return values

    @model_validator(mode="after")
    def _decision(self) -> ControlRunCompatibilityReceiptV1:
        predicates = (
            self.control_task_manifest_identical,
            self.stream_order_identical,
            self.model_reasoning_codex_identical,
            self.prompt_parser_evaluator_identical,
            self.three_sessions_per_task_identical,
            self.executor_success_semantics_proven_identical,
            self.control_validator_absent,
            self.control_evolution_absent,
            self.predictions_replay_identical,
            self.historical_run_complete,
            self.result_digest_chain_closed,
            self.source_diff_allowlisted,
        )
        compatible = all(predicates) and not self.finding_codes
        if (self.status == "COMPATIBLE") is not compatible:
            raise ValueError("compatibility status disagrees with direct predicates")
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self)


def audit_control_run_compatibility_v1(
    repository_root: Path,
    *,
    generation_id: str,
    attempt_id: str,
) -> ControlRunCompatibilityReceiptV1:
    """Audit immutable old evidence; never mutate or replay a paid invocation."""

    root = repository_root.resolve()
    if (
        re.fullmatch(r"gen_[0-9a-f]{64}", generation_id) is None
        or re.fullmatch(r"attempt_[0-9]{6}", attempt_id) is None
    ):
        raise ValueError("historical Control500 authority identity is invalid")
    attempt_root = (
        root
        / "results"
        / "chembench4k_taskwise_online_v1"
        / "pilot500_generations"
        / generation_id
        / "attempts"
        / attempt_id
    )
    suite = _read_json(attempt_root / "control_suite_state.json", private=False)
    current_commit = _git(root, "rev-parse", "HEAD").strip()
    states: list[dict[str, Any]] = []
    public_chains: list[str] = []
    private_chains: list[str] = []
    private_rows_all: list[dict[str, Any]] = []
    public_private_parity = True
    parser_replay = True
    manifest_identity = True
    config_semantics = True
    model_identity = True
    sessions_identity = True
    success_receipt_count = 0
    success_receipts_valid = True
    for stream_index in range(_STREAM_COUNT):
        scope = f"pilot500_stream_{stream_index:02d}"
        control_root = attempt_root / scope / "control"
        state = _read_json(control_root / "run_state.json", private=False)
        states.append(state)
        public_rows = _read_jsonl(control_root / "public" / "events.jsonl", private=False)
        private_rows = _read_jsonl(
            control_root / "private" / "evaluations.jsonl",
            private=True,
        )
        private_rows_all.extend(private_rows)
        public_chain = _chain_rows(public_rows)
        private_chain = _chain_rows(private_rows)
        public_chains.append(public_chain)
        private_chains.append(private_chain)
        public_private_parity &= (
            public_chain == state.get("public_event_chain_sha256")
            and private_chain == state.get("private_evaluation_chain_sha256")
            and len(public_rows) == len(private_rows) == _TASKS_PER_STREAM * _SESSIONS_PER_TASK
            and all(
                _public_private_row_identity(public, private)
                for public, private in zip(public_rows, private_rows, strict=True)
            )
        )
        parser_replay &= all(
            official_first_capital_parser(str(row.get("raw_completion", ""))).prediction
            == row.get("official_prediction")
            for row in private_rows
        )
        binding = state.get("binding")
        if not isinstance(binding, dict):
            binding = {}
        manifest_path = (
            root
            / "benchmarks"
            / "chembench"
            / "manifests"
            / "taskwise_online_v1"
            / "streams"
            / f"stream_{stream_index:02d}_public_manifest.jsonl"
        )
        manifest_identity &= _sha256_bytes(manifest_path.read_bytes()) == binding.get(
            "task_manifest_sha256"
        )
        current_config_path = (
            root
            / "benchmarks"
            / "chembench"
            / "configs"
            / f"control_{scope}_taskwise_online_v1.yaml"
        )
        current_config = _read_yaml(current_config_path.read_text(encoding="utf-8"))
        historical_commit = str(binding.get("source_commit", ""))
        historical_config = _read_yaml(
            _git(
                root,
                "show",
                f"{historical_commit}:benchmarks/chembench/configs/"
                f"control_{scope}_taskwise_online_v1.yaml",
            )
        )
        config_semantics &= all(
            current_config.get(field) == historical_config.get(field)
            for field in _CONFIG_SEMANTIC_FIELDS
        )
        model_identity &= (
            binding.get("model") == current_config.get("model") == "gpt-5.5"
            and binding.get("reasoning_effort")
            == current_config.get("reasoning_effort")
            == "medium"
        )
        sessions_identity &= (
            current_config.get("attempts_per_task") == _SESSIONS_PER_TASK
            and state.get("session_attempt_count") == _TASKS_PER_STREAM * _SESSIONS_PER_TASK
        )
        diagnostic_root = (
            root
            / "benchmarks"
            / "chembench"
            / "state"
            / "taskwise_online_v1"
            / "private_executor_events"
            / scope
            / f"taskwise_control_{scope}_{generation_id}_{attempt_id}"
            / "control"
        )
        success_paths = sorted(diagnostic_root.glob("success_*.json"))
        success_receipt_count += len(success_paths)
        for path in success_paths:
            receipt = _read_json(path, private=True)
            success_receipts_valid &= (
                receipt.get("status") == "COMPLETED"
                and receipt.get("codex_cli_version") == current_config.get("codex_cli_version")
                and receipt.get("model") == "gpt-5.5"
                and receipt.get("tool_event_count") == 0
                and receipt.get("cleanup_status") == "COMPLETE"
                and receipt.get("residual_root_count") == 0
            )

    historical_commits = {
        str(state.get("binding", {}).get("source_commit", "")) for state in states
    }
    if len(historical_commits) != 1:
        raise ValueError("historical Control500 source binding is inconsistent")
    historical_commit = next(iter(historical_commits))
    changed_files = tuple(
        line
        for line in _git(
            root,
            "diff",
            "--name-only",
            f"{historical_commit}..{current_commit}",
            "--",
            "benchmarks/chembench",
        ).splitlines()
        if line
    )
    disallowed = tuple(path for path in changed_files if path not in _SAFE_SOURCE_DIFF)
    executor_changed = any(
        path.endswith("/local_codex_executor.py") or path.endswith("/taskwise_online_runner_v1.py")
        for path in changed_files
    )
    completed_tasks = sum(int(state.get("completed_tasks", -1)) for state in states)
    completed_sessions = sum(int(state.get("completion_count", -1)) for state in states)
    historical_complete = (
        suite.get("status") == "COMPLETED"
        and suite.get("completed_streams") == _STREAM_COUNT
        and len(states) == _STREAM_COUNT
        and all(state.get("status") == "COMPLETED" for state in states)
        and completed_tasks == _STREAM_COUNT * _TASKS_PER_STREAM
        and completed_sessions == _STREAM_COUNT * _TASKS_PER_STREAM * _SESSIONS_PER_TASK
        and success_receipt_count == completed_sessions
        and success_receipts_valid
    )
    control_evolution_absent = all(
        state.get("update_count") == 0
        and state.get("core_job_count") == 0
        and state.get("core_artifact_count") == 0
        and state.get("context_resolution_count", 0) == 0
        for state in states
    )
    counters = {
        "infrastructure_failures": int(suite.get("infrastructure_failures", -1)),
        "security_violations": int(suite.get("security_violations", -1)),
        "context_binding_violations": int(suite.get("context_binding_violations", -1)),
        "contract_violations": int(suite.get("meeting722_contract_violations", 0)),
    }
    finding_codes: set[str] = set()
    checks = {
        "control_task_manifest_identical": manifest_identity,
        "stream_order_identical": manifest_identity and config_semantics,
        "model_reasoning_codex_identical": model_identity and success_receipts_valid,
        "prompt_parser_evaluator_identical": config_semantics,
        "three_sessions_per_task_identical": sessions_identity,
        "executor_success_semantics_proven_identical": not executor_changed,
        "control_validator_absent": control_evolution_absent,
        "control_evolution_absent": control_evolution_absent,
        "predictions_replay_identical": parser_replay,
        "historical_run_complete": historical_complete and not any(counters.values()),
        "result_digest_chain_closed": public_private_parity,
        "source_diff_allowlisted": not disallowed,
    }
    finding_map = {
        "control_task_manifest_identical": "CONTROL_TASK_MANIFEST_MISMATCH",
        "stream_order_identical": "CONTROL_STREAM_ORDER_MISMATCH",
        "model_reasoning_codex_identical": "CONTROL_MODEL_IDENTITY_MISMATCH",
        "prompt_parser_evaluator_identical": "CONTROL_EVALUATION_SEMANTICS_MISMATCH",
        "three_sessions_per_task_identical": "CONTROL_SESSION_BUDGET_MISMATCH",
        "executor_success_semantics_proven_identical": "CONTROL_EXECUTOR_SEMANTICS_UNPROVEN",
        "control_validator_absent": "CONTROL_VALIDATOR_PATH_OBSERVED",
        "control_evolution_absent": "CONTROL_EVOLUTION_PATH_OBSERVED",
        "predictions_replay_identical": "CONTROL_PREDICTION_REPLAY_MISMATCH",
        "historical_run_complete": "CONTROL_RUN_INCOMPLETE",
        "result_digest_chain_closed": "CONTROL_RESULT_CHAIN_INVALID",
        "source_diff_allowlisted": "CONTROL_SOURCE_DIFF_NOT_ALLOWLISTED",
    }
    finding_codes.update(code for field, code in finding_map.items() if not checks[field])
    return ControlRunCompatibilityReceiptV1(
        status="COMPATIBLE" if all(checks.values()) else "INCOMPATIBLE",
        historical_source_commit=historical_commit,
        current_source_commit=current_commit,
        historical_generation_id_sha256=_sha256_bytes(generation_id.encode("utf-8")),
        historical_attempt_id_sha256=_sha256_bytes(attempt_id.encode("utf-8")),
        **checks,
        completed_streams=int(suite.get("completed_streams", -1)),
        completed_tasks=completed_tasks,
        completed_sessions=completed_sessions,
        success_receipt_count=success_receipt_count,
        **counters,
        changed_file_count=len(changed_files),
        disallowed_changed_file_count=len(disallowed),
        public_chain_set_sha256=_sha256_json(public_chains),
        private_chain_set_sha256=_sha256_json(private_chains),
        result_evidence_sha256=_sha256_json(private_rows_all),
        source_diff_sha256=_sha256_json(changed_files),
        allowlist_sha256=_sha256_json(sorted(_SAFE_SOURCE_DIFF)),
        finding_codes=tuple(sorted(finding_codes)),
    )


def write_control_compatibility_receipt_v1(
    path: Path,
    receipt: ControlRunCompatibilityReceiptV1,
) -> None:
    """Exclusively persist one immutable mode-0600 compatibility decision."""

    encoded = _canonical_bytes(receipt.model_dump(mode="json"))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def _public_private_row_identity(public: dict[str, Any], private: dict[str, Any]) -> bool:
    fields = (
        "arm",
        "category",
        "official_parse_status",
        "official_prediction",
        "round_index",
        "task_ordinal",
        "task_uid",
    )
    return all(public.get(field) == private.get(field) for field in fields)


def _read_json(path: Path, *, private: bool) -> dict[str, Any]:
    if private and stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ValueError("private compatibility evidence permissions are invalid")
    value = json.loads(path.read_bytes())
    if type(value) is not dict:
        raise ValueError("compatibility evidence must be a JSON object")
    return value


def _read_jsonl(path: Path, *, private: bool) -> list[dict[str, Any]]:
    if private and stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ValueError("private compatibility evidence permissions are invalid")
    rows = [json.loads(line) for line in path.read_bytes().splitlines() if line]
    if any(type(row) is not dict for row in rows):
        raise ValueError("compatibility JSONL evidence is invalid")
    return rows


def _read_yaml(value: str) -> dict[str, Any]:
    payload = yaml.safe_load(value)
    if type(payload) is not dict:
        raise ValueError("compatibility config evidence is invalid")
    return payload


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", *arguments),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: object) -> str:
    return _sha256_bytes(_canonical_bytes(value))


__all__ = [
    "ControlRunCompatibilityReceiptV1",
    "audit_control_run_compatibility_v1",
    "write_control_compatibility_receipt_v1",
]
