"""Strict configuration contracts for the frozen ChemBench4K v2 protocol."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml


PROTOCOL_ID = "chembench4k_frozen_generalization_v2"
EXECUTION_MODE = "standalone_openevo_maintainer_benchmark"
DATASET_REPOSITORY = "AI4Chem/ChemBench4K"
DATASET_REVISION = "f8ad41a980170f4c5d0cc97e57722d06887c8f53"
MODEL = "gpt-5.5"
REASONING_EFFORT = "medium"
CONFIG_SCHEMA = "chembench4k_frozen_config_v2"
PLACEHOLDER = "REQUIRED_AT_FREEZE"

_SHA256 = re.compile(r"[0-9a-f]{64}")
_ARMS = frozenset({"baseline", "evolved"})
_SCOPES = frozenset({"canary18", "pilot500", "full"})


class FrozenConfigError(ValueError):
    """Raised when a formal v2 configuration is not closed and reproducible."""


@dataclass(frozen=True, slots=True)
class FrozenArtifactBinding:
    enabled: bool
    frozen_artifact_id: str | None
    frozen_artifact_sha256: str | None
    context_resolution_digest: str | None
    resolved_memory_sha256: str | None

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise TypeError("artifact.enabled must be boolean")
        values = (
            self.frozen_artifact_id,
            self.frozen_artifact_sha256,
            self.context_resolution_digest,
            self.resolved_memory_sha256,
        )
        if not self.enabled:
            if any(value is not None for value in values):
                raise FrozenConfigError("baseline must not bind any evolution artifact")
            return
        if any(type(value) is not str or not value for value in values):
            raise FrozenConfigError("evolved arm must bind every frozen artifact field")
        for value, field_name in (
            (self.frozen_artifact_sha256, "frozen_artifact_sha256"),
            (self.context_resolution_digest, "context_resolution_digest"),
            (self.resolved_memory_sha256, "resolved_memory_sha256"),
        ):
            if value != PLACEHOLDER and _SHA256.fullmatch(str(value)) is None:
                raise FrozenConfigError(f"{field_name} must be SHA-256 or freeze placeholder")

    @property
    def is_frozen(self) -> bool:
        return self.enabled and all(
            value != PLACEHOLDER
            for value in (
                self.frozen_artifact_id,
                self.frozen_artifact_sha256,
                self.context_resolution_digest,
                self.resolved_memory_sha256,
            )
        )


@dataclass(frozen=True, slots=True)
class ExecutorPolicyV2:
    backend: str
    harness: str
    timeout_seconds: int
    concurrency: int
    infrastructure_retries_before_completion: int
    tools_enabled: bool
    mcp_enabled: bool
    web_enabled: bool
    network_enabled: bool
    subagents_enabled: bool

    def __post_init__(self) -> None:
        if self.backend != "local_codex_cli" or self.harness != "codex_cli":
            raise FrozenConfigError("v2 requires the local Codex CLI backend")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int)
            or self.timeout_seconds < 1
        ):
            raise FrozenConfigError("executor timeout_seconds must be positive")
        if self.concurrency != 1:
            raise FrozenConfigError("v2 concurrency is frozen at one")
        if self.infrastructure_retries_before_completion != 0:
            raise FrozenConfigError("v2 performs exactly one model invocation per item")
        for name in (
            "tools_enabled",
            "mcp_enabled",
            "web_enabled",
            "network_enabled",
            "subagents_enabled",
        ):
            if getattr(self, name) is not False:
                raise FrozenConfigError(f"executor {name} must remain false")


@dataclass(frozen=True, slots=True)
class FrozenExperimentConfigV2:
    """One arm of the immutable paired frozen-generalization protocol."""

    arm: str
    scope: str
    run_name: str
    output_directory: str
    dataset_root: str
    dataset_manifest: str
    task_manifest: str
    private_task_manifest: str
    receipt_path: str
    pilot_protocol_hash: str
    codex_cli_version: str
    prompt_renderer_id: str
    parser_id: str
    evaluator_id: str
    artifact: FrozenArtifactBinding
    executor: ExecutorPolicyV2
    schema_version: str = CONFIG_SCHEMA
    protocol_id: str = PROTOCOL_ID
    execution_mode: str = EXECUTION_MODE
    dataset_repository: str = DATASET_REPOSITORY
    dataset_revision: str = DATASET_REVISION
    model: str = MODEL
    reasoning_effort: str = REASONING_EFFORT

    def __post_init__(self) -> None:
        if self.schema_version != CONFIG_SCHEMA:
            raise FrozenConfigError("unexpected config schema")
        if self.protocol_id != PROTOCOL_ID:
            raise FrozenConfigError("unexpected protocol_id")
        if self.execution_mode != EXECUTION_MODE:
            raise FrozenConfigError("unexpected execution_mode")
        if self.dataset_repository != DATASET_REPOSITORY:
            raise FrozenConfigError("unexpected dataset repository")
        if self.dataset_revision != DATASET_REVISION:
            raise FrozenConfigError("unexpected dataset revision")
        if self.model != MODEL or self.reasoning_effort != REASONING_EFFORT:
            raise FrozenConfigError("model identity is not frozen")
        if self.arm not in _ARMS or self.scope not in _SCOPES:
            raise FrozenConfigError("unknown arm or scope")
        if self.artifact.enabled != (self.arm == "evolved"):
            raise FrozenConfigError("artifact treatment must agree with arm")
        for value, field_name in (
            (self.run_name, "run_name"),
            (self.output_directory, "output_directory"),
            (self.dataset_root, "dataset_root"),
            (self.dataset_manifest, "dataset_manifest"),
            (self.task_manifest, "task_manifest"),
            (self.private_task_manifest, "private_task_manifest"),
            (self.receipt_path, "receipt_path"),
            (self.codex_cli_version, "codex_cli_version"),
            (self.prompt_renderer_id, "prompt_renderer_id"),
            (self.parser_id, "parser_id"),
            (self.evaluator_id, "evaluator_id"),
        ):
            if type(value) is not str or not value:
                raise FrozenConfigError(f"{field_name} must be non-empty text")
        for value, field_name in (
            (self.output_directory, "output_directory"),
            (self.dataset_root, "dataset_root"),
            (self.dataset_manifest, "dataset_manifest"),
            (self.task_manifest, "task_manifest"),
            (self.private_task_manifest, "private_task_manifest"),
            (self.receipt_path, "receipt_path"),
        ):
            path = PurePosixPath(value)
            if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
                raise FrozenConfigError(f"{field_name} must be a clean relative path")
        if self.pilot_protocol_hash != PLACEHOLDER and (
            _SHA256.fullmatch(self.pilot_protocol_hash) is None
        ):
            raise FrozenConfigError("pilot_protocol_hash must be SHA-256 or placeholder")

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "protocol_id": self.protocol_id,
            "execution_mode": self.execution_mode,
            "arm": self.arm,
            "scope": self.scope,
            "run_name": self.run_name,
            "output_directory": self.output_directory,
            "dataset": {
                "repository": self.dataset_repository,
                "revision": self.dataset_revision,
                "root": self.dataset_root,
                "manifest": self.dataset_manifest,
                "task_manifest": self.task_manifest,
                "private_task_manifest": self.private_task_manifest,
            },
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "codex_cli_version": self.codex_cli_version,
            "prompt_renderer_id": self.prompt_renderer_id,
            "parser_id": self.parser_id,
            "evaluator_id": self.evaluator_id,
            "evolution": {
                "enabled": self.artifact.enabled,
                "target": "text_memory",
                "skill_bundle": False,
                "agent_system": False,
                "frozen_artifact_id": self.artifact.frozen_artifact_id,
                "frozen_artifact_sha256": self.artifact.frozen_artifact_sha256,
                "context_resolution_digest": self.artifact.context_resolution_digest,
                "resolved_memory_sha256": self.artifact.resolved_memory_sha256,
            },
            "executor": {
                "backend": self.executor.backend,
                "harness": self.executor.harness,
                "timeout_seconds": self.executor.timeout_seconds,
                "concurrency": self.executor.concurrency,
                "infrastructure_retries_before_completion": (
                    self.executor.infrastructure_retries_before_completion
                ),
                "tools_enabled": self.executor.tools_enabled,
                "mcp_enabled": self.executor.mcp_enabled,
                "web_enabled": self.executor.web_enabled,
                "network_enabled": self.executor.network_enabled,
                "subagents_enabled": self.executor.subagents_enabled,
            },
            "receipt_path": self.receipt_path,
            "pilot_protocol_hash": self.pilot_protocol_hash,
        }

    def config_sha256(self) -> str:
        return hashlib.sha256(canonical_config_bytes(self.to_payload())).hexdigest()


def canonical_config_bytes(payload: dict[str, object]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _closed_mapping(
    value: object,
    *,
    keys: frozenset[str],
    field_name: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise FrozenConfigError(f"{field_name} must contain exactly {sorted(keys)}")
    if not all(type(key) is str for key in value):
        raise FrozenConfigError(f"{field_name} keys must be strings")
    return dict(value)


def load_frozen_config_v2(path: Path) -> FrozenExperimentConfigV2:
    """Load a closed v2 YAML without accepting legacy config fields."""

    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise FrozenConfigError("failed to read frozen v2 config") from exc
    root = _closed_mapping(
        loaded,
        keys=frozenset(
            {
                "schema_version",
                "protocol_id",
                "execution_mode",
                "arm",
                "scope",
                "run_name",
                "output_directory",
                "dataset",
                "model",
                "reasoning_effort",
                "codex_cli_version",
                "prompt_renderer_id",
                "parser_id",
                "evaluator_id",
                "evolution",
                "executor",
                "receipt_path",
                "pilot_protocol_hash",
            }
        ),
        field_name="config",
    )
    dataset = _closed_mapping(
        root["dataset"],
        keys=frozenset(
            {
                "repository",
                "revision",
                "root",
                "manifest",
                "task_manifest",
                "private_task_manifest",
            }
        ),
        field_name="dataset",
    )
    evolution = _closed_mapping(
        root["evolution"],
        keys=frozenset(
            {
                "enabled",
                "target",
                "skill_bundle",
                "agent_system",
                "frozen_artifact_id",
                "frozen_artifact_sha256",
                "context_resolution_digest",
                "resolved_memory_sha256",
            }
        ),
        field_name="evolution",
    )
    if (
        evolution["target"] != "text_memory"
        or evolution["skill_bundle"] is not False
        or evolution["agent_system"] is not False
    ):
        raise FrozenConfigError("only text_memory evolution is enabled in v2")
    executor = _closed_mapping(
        root["executor"],
        keys=frozenset(
            {
                "backend",
                "harness",
                "timeout_seconds",
                "concurrency",
                "infrastructure_retries_before_completion",
                "tools_enabled",
                "mcp_enabled",
                "web_enabled",
                "network_enabled",
                "subagents_enabled",
            }
        ),
        field_name="executor",
    )
    return FrozenExperimentConfigV2(
        schema_version=root["schema_version"],
        protocol_id=root["protocol_id"],
        execution_mode=root["execution_mode"],
        arm=root["arm"],
        scope=root["scope"],
        run_name=root["run_name"],
        output_directory=root["output_directory"],
        dataset_repository=dataset["repository"],
        dataset_revision=dataset["revision"],
        dataset_root=dataset["root"],
        dataset_manifest=dataset["manifest"],
        task_manifest=dataset["task_manifest"],
        private_task_manifest=dataset["private_task_manifest"],
        model=root["model"],
        reasoning_effort=root["reasoning_effort"],
        codex_cli_version=root["codex_cli_version"],
        prompt_renderer_id=root["prompt_renderer_id"],
        parser_id=root["parser_id"],
        evaluator_id=root["evaluator_id"],
        artifact=FrozenArtifactBinding(
            enabled=evolution["enabled"],
            frozen_artifact_id=evolution["frozen_artifact_id"],
            frozen_artifact_sha256=evolution["frozen_artifact_sha256"],
            context_resolution_digest=evolution["context_resolution_digest"],
            resolved_memory_sha256=evolution["resolved_memory_sha256"],
        ),
        executor=ExecutorPolicyV2(**executor),
        receipt_path=root["receipt_path"],
        pilot_protocol_hash=root["pilot_protocol_hash"],
    )


def arm_parity_findings(
    baseline: FrozenExperimentConfigV2,
    evolved: FrozenExperimentConfigV2,
) -> tuple[str, ...]:
    """Return closed finding codes for forbidden paired-arm differences."""

    if baseline.arm != "baseline" or evolved.arm != "evolved":
        return ("ARM_IDENTITY_INVALID",)
    left = baseline.to_payload()
    right = evolved.to_payload()
    for payload in (left, right):
        payload.pop("arm")
        payload.pop("run_name")
        payload.pop("output_directory")
        evolution = dict(payload["evolution"])
        for key in (
            "enabled",
            "frozen_artifact_id",
            "frozen_artifact_sha256",
            "context_resolution_digest",
            "resolved_memory_sha256",
        ):
            evolution.pop(key)
        payload["evolution"] = evolution
    if left == right:
        return ()
    return ("ARM_PARITY_MISMATCH",)


__all__ = [
    "CONFIG_SCHEMA",
    "DATASET_REPOSITORY",
    "DATASET_REVISION",
    "EXECUTION_MODE",
    "ExecutorPolicyV2",
    "FrozenArtifactBinding",
    "FrozenConfigError",
    "FrozenExperimentConfigV2",
    "MODEL",
    "PLACEHOLDER",
    "PROTOCOL_ID",
    "REASONING_EFFORT",
    "arm_parity_findings",
    "canonical_config_bytes",
    "load_frozen_config_v2",
]
