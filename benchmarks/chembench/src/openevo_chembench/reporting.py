"""Allowlisted online-evolution result models and atomic persistence."""

from __future__ import annotations

import json
import math
import os
import re
import secrets
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from openevo_chembench.artifacts import (
    ArtifactKind,
    ValidationReceipt,
)
from openevo_chembench.models import AgentRuntimeMetadata, ParseStatus
from openevo_chembench.runtime_context import AgentArtifactContext


_SHA256 = re.compile(r"[0-9a-f]{64}")
_RUNTIME_ID = re.compile(r"(?:run|episode)_[0-9a-f]{24}")
_COMMIT_HASH = re.compile(r"[0-9a-f]{40}")


def _require_non_empty_text(value: object, field_name: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def _require_sha256(value: object, field_name: str) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")


def _require_runtime_id(value: object, field_name: str) -> None:
    if type(value) is not str or _RUNTIME_ID.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a controller-generated runtime id")


def _require_non_negative_integer(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")


class RunMode(str, Enum):
    """Experiment mode derived solely from ``max_rounds``."""

    BASELINE = "baseline"
    EVOLUTION = "evolution"


class RoundStatus(str, Enum):
    """Closed state for one persisted round."""

    EVALUATED = "evaluated"
    EVOLVED = "evolved"
    ARTIFACT_REJECTED = "artifact_rejected"


class EpisodeStatus(str, Enum):
    """Closed terminal episode state."""

    COMPLETED = "completed"
    ARTIFACT_REJECTED = "artifact_rejected"


@dataclass(frozen=True, slots=True)
class RunManifest:
    """Answer-free, revision-pinned identity for one result directory."""

    run_id: str
    protocol_id: str
    mode: RunMode
    max_rounds: int
    openevo_revision: str
    chembench_revision: str
    seed: int
    prompt_version: str
    codex_cli_version: str
    harness: str
    model: str
    capture_mode: str
    execution_backend: str
    enabled_artifact_types: tuple[ArtifactKind, ...]

    def __post_init__(self) -> None:
        _require_runtime_id(self.run_id, "RunManifest.run_id")
        _require_non_empty_text(self.protocol_id, "RunManifest.protocol_id")
        if type(self.mode) is not RunMode:
            raise TypeError("RunManifest.mode must be RunMode")
        _require_non_negative_integer(self.max_rounds, "RunManifest.max_rounds")
        if (self.max_rounds == 0) != (self.mode is RunMode.BASELINE):
            raise ValueError("RunManifest.mode must agree with max_rounds")
        for value, field_name in (
            (self.openevo_revision, "RunManifest.openevo_revision"),
            (self.chembench_revision, "RunManifest.chembench_revision"),
        ):
            if type(value) is not str or _COMMIT_HASH.fullmatch(value) is None:
                raise ValueError(f"{field_name} must be a full commit hash")
        _require_non_negative_integer(self.seed, "RunManifest.seed")
        for value, field_name in (
            (self.prompt_version, "RunManifest.prompt_version"),
            (self.codex_cli_version, "RunManifest.codex_cli_version"),
            (self.harness, "RunManifest.harness"),
            (self.model, "RunManifest.model"),
            (self.capture_mode, "RunManifest.capture_mode"),
            (self.execution_backend, "RunManifest.execution_backend"),
        ):
            _require_non_empty_text(value, field_name)
        if self.execution_backend not in {
            "managed_codex",
            "local_codex_cli",
        }:
            raise ValueError("RunManifest.execution_backend is unsupported")
        expected_harness = (
            "codex"
            if self.execution_backend == "managed_codex"
            else "codex_cli"
        )
        if self.harness != expected_harness:
            raise ValueError(
                "RunManifest harness must agree with execution backend"
            )
        if not isinstance(self.enabled_artifact_types, tuple) or not all(
            type(kind) is ArtifactKind for kind in self.enabled_artifact_types
        ):
            raise TypeError(
                "RunManifest.enabled_artifact_types must contain ArtifactKind values"
            )
        if len(self.enabled_artifact_types) != len(
            set(self.enabled_artifact_types)
        ):
            raise ValueError("RunManifest.enabled_artifact_types must be unique")

    def to_result_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "protocol_id": self.protocol_id,
            "mode": self.mode.value,
            "max_rounds": self.max_rounds,
            "openevo_revision": self.openevo_revision,
            "chembench_revision": self.chembench_revision,
            "seed": self.seed,
            "prompt_version": self.prompt_version,
            "codex_cli_version": self.codex_cli_version,
            "agent": {
                "harness": self.harness,
                "model": self.model,
                "capture_mode": self.capture_mode,
                "execution_backend": self.execution_backend,
            },
            "enabled_artifact_types": [
                kind.value for kind in self.enabled_artifact_types
            ],
        }


@dataclass(frozen=True, slots=True)
class AppliedArtifactReference:
    """Content-free reference to an artifact used during one round."""

    artifact_type: ArtifactKind
    artifact_hash: str
    artifact_version: int
    source_round_index: int

    def __post_init__(self) -> None:
        if type(self.artifact_type) is not ArtifactKind:
            raise TypeError("AppliedArtifactReference.artifact_type must be ArtifactKind")
        _require_sha256(self.artifact_hash, "AppliedArtifactReference.artifact_hash")
        if (
            isinstance(self.artifact_version, bool)
            or not isinstance(self.artifact_version, int)
            or self.artifact_version < 1
        ):
            raise ValueError(
                "AppliedArtifactReference.artifact_version must be positive"
            )
        _require_non_negative_integer(
            self.source_round_index,
            "AppliedArtifactReference.source_round_index",
        )

    @classmethod
    def from_context(
        cls,
        context: AgentArtifactContext,
    ) -> AppliedArtifactReference:
        if type(context) is not AgentArtifactContext:
            raise TypeError("context must be exact AgentArtifactContext")
        return cls(
            artifact_type=context.artifact_type,
            artifact_hash=context.artifact_hash,
            artifact_version=context.artifact_version,
            source_round_index=context.source_round_index,
        )

    def to_result_payload(self) -> dict[str, object]:
        return {
            "artifact_type": self.artifact_type.value,
            "artifact_hash": self.artifact_hash,
            "artifact_version": self.artifact_version,
            "source_round": self.source_round_index,
        }


@dataclass(frozen=True, slots=True)
class ArtifactHistoryRecord:
    """Answer-free validation and activation history for one proposal."""

    artifact_type: ArtifactKind
    artifact_version: int
    source_round_index: int
    target_round_index: int
    parent_hash: str | None
    candidate_hash: str
    approved_artifact_hash: str | None
    validator_receipt: ValidationReceipt
    activated_for_next_round: bool

    def __post_init__(self) -> None:
        if type(self.artifact_type) is not ArtifactKind:
            raise TypeError("ArtifactHistoryRecord.artifact_type must be ArtifactKind")
        if (
            isinstance(self.artifact_version, bool)
            or not isinstance(self.artifact_version, int)
            or self.artifact_version < 1
        ):
            raise ValueError("ArtifactHistoryRecord.artifact_version must be positive")
        _require_non_negative_integer(
            self.source_round_index,
            "ArtifactHistoryRecord.source_round_index",
        )
        if self.target_round_index != self.source_round_index + 1:
            raise ValueError(
                "ArtifactHistoryRecord.target_round_index must be source round plus one"
            )
        if self.parent_hash is not None:
            _require_sha256(self.parent_hash, "ArtifactHistoryRecord.parent_hash")
        _require_sha256(self.candidate_hash, "ArtifactHistoryRecord.candidate_hash")
        if self.approved_artifact_hash is not None:
            _require_sha256(
                self.approved_artifact_hash,
                "ArtifactHistoryRecord.approved_artifact_hash",
            )
        if type(self.validator_receipt) is not ValidationReceipt:
            raise TypeError(
                "ArtifactHistoryRecord.validator_receipt must be ValidationReceipt"
            )
        if self.validator_receipt.candidate_hash != self.candidate_hash:
            raise ValueError("artifact history receipt must match candidate hash")
        if not isinstance(self.activated_for_next_round, bool):
            raise TypeError(
                "ArtifactHistoryRecord.activated_for_next_round must be boolean"
            )
        if self.validator_receipt.passed != (
            self.approved_artifact_hash is not None
        ):
            raise ValueError(
                "artifact history approval hash must match receipt decision"
            )
        if self.activated_for_next_round and not self.validator_receipt.passed:
            raise ValueError("rejected artifact cannot be activated")

    def to_result_payload(self) -> dict[str, object]:
        return {
            "artifact_type": self.artifact_type.value,
            "artifact_version": self.artifact_version,
            "source_round": self.source_round_index,
            "target_round": self.target_round_index,
            "parent_hash": self.parent_hash,
            "candidate_hash": self.candidate_hash,
            "approved_artifact_hash": self.approved_artifact_hash,
            "activated_for_next_round": self.activated_for_next_round,
            "validator_receipt": self.validator_receipt.to_audit_payload(),
        }


@dataclass(frozen=True, slots=True)
class RoundResult:
    """Persistable, answer-free result for one evaluated round."""

    round_index: int
    score: float
    parse_status: ParseStatus
    status: RoundStatus
    applied_artifacts: tuple[AppliedArtifactReference, ...] = ()
    validator_receipts: tuple[ValidationReceipt, ...] = ()
    runtime_metadata: AgentRuntimeMetadata | None = None

    def __post_init__(self) -> None:
        _require_non_negative_integer(self.round_index, "RoundResult.round_index")
        if (
            isinstance(self.score, bool)
            or not isinstance(self.score, (int, float))
            or not math.isfinite(float(self.score))
        ):
            raise ValueError("RoundResult.score must be finite")
        if type(self.parse_status) is not ParseStatus:
            raise TypeError("RoundResult.parse_status must be ParseStatus")
        if type(self.status) is not RoundStatus:
            raise TypeError("RoundResult.status must be RoundStatus")
        if not isinstance(self.applied_artifacts, tuple) or not all(
            type(reference) is AppliedArtifactReference
            for reference in self.applied_artifacts
        ):
            raise TypeError(
                "RoundResult.applied_artifacts must contain exact references"
            )
        if not isinstance(self.validator_receipts, tuple) or not all(
            type(receipt) is ValidationReceipt
            for receipt in self.validator_receipts
        ):
            raise TypeError(
                "RoundResult.validator_receipts must contain exact receipts"
            )
        if (
            self.runtime_metadata is not None
            and type(self.runtime_metadata) is not AgentRuntimeMetadata
        ):
            raise TypeError(
                "RoundResult.runtime_metadata must be exact AgentRuntimeMetadata or None"
            )
        if self.status is RoundStatus.EVALUATED and self.validator_receipts:
            raise ValueError("final evaluated round cannot contain validator receipts")
        if (
            self.status is RoundStatus.ARTIFACT_REJECTED
            and not any(not receipt.passed for receipt in self.validator_receipts)
        ):
            raise ValueError("artifact-rejected round requires a rejected receipt")
        if (
            self.status is RoundStatus.EVOLVED
            and (
                not self.validator_receipts
                or not all(receipt.passed for receipt in self.validator_receipts)
            )
        ):
            raise ValueError("evolved round requires only passing receipts")

    def to_result_payload(
        self,
        *,
        run_id: str,
        episode_id: str,
        mode: RunMode,
    ) -> dict[str, object]:
        _require_runtime_id(run_id, "RoundResult.run_id")
        _require_runtime_id(episode_id, "RoundResult.episode_id")
        if type(mode) is not RunMode:
            raise TypeError("RoundResult.mode must be RunMode")
        payload: dict[str, object] = {
            "schema_version": 1,
            "run_id": run_id,
            "episode_id": episode_id,
            "mode": mode.value,
            "round": self.round_index,
            "status": self.status.value,
            "score": float(self.score),
            "parse_status": self.parse_status.value,
            "applied_artifacts": [
                artifact.to_result_payload() for artifact in self.applied_artifacts
            ],
            "validator_receipts": [
                receipt.to_audit_payload() for receipt in self.validator_receipts
            ],
        }
        if self.runtime_metadata is not None:
            payload["runtime"] = self.runtime_metadata.to_result_payload()
        return payload


@dataclass(frozen=True, slots=True)
class EpisodeResult:
    """Safe terminal episode summary returned by the orchestration layer."""

    run_id: str
    episode_id: str
    mode: RunMode
    status: EpisodeStatus
    rounds: tuple[RoundResult, ...]
    artifact_history: tuple[ArtifactHistoryRecord, ...]

    def __post_init__(self) -> None:
        _require_runtime_id(self.run_id, "EpisodeResult.run_id")
        _require_runtime_id(self.episode_id, "EpisodeResult.episode_id")
        if type(self.mode) is not RunMode:
            raise TypeError("EpisodeResult.mode must be RunMode")
        if type(self.status) is not EpisodeStatus:
            raise TypeError("EpisodeResult.status must be EpisodeStatus")
        if not isinstance(self.rounds, tuple) or not self.rounds:
            raise ValueError("EpisodeResult.rounds must be a non-empty tuple")
        if not all(type(result) is RoundResult for result in self.rounds):
            raise TypeError("EpisodeResult.rounds must contain exact RoundResult values")
        if tuple(result.round_index for result in self.rounds) != tuple(
            range(len(self.rounds))
        ):
            raise ValueError("EpisodeResult.rounds must be contiguous from round zero")
        if not isinstance(self.artifact_history, tuple) or not all(
            type(record) is ArtifactHistoryRecord
            for record in self.artifact_history
        ):
            raise TypeError(
                "EpisodeResult.artifact_history must contain exact history records"
            )
        last_status = self.rounds[-1].status
        if (
            self.status is EpisodeStatus.COMPLETED
            and last_status is not RoundStatus.EVALUATED
        ):
            raise ValueError("completed episode must end with an evaluated round")
        if (
            self.status is EpisodeStatus.ARTIFACT_REJECTED
            and last_status is not RoundStatus.ARTIFACT_REJECTED
        ):
            raise ValueError(
                "artifact-rejected episode must end with an artifact rejection"
            )

    @property
    def final_score(self) -> float:
        return float(self.rounds[-1].score)


class _ResultStore:
    """Write only strict result DTO payloads into a new run directory."""

    __slots__ = ("root", "_episodes_root")

    def __init__(self, root: Path) -> None:
        if not isinstance(root, Path):
            raise TypeError("result root must be pathlib.Path")
        if root.exists():
            raise FileExistsError("result root must not already exist")
        root.mkdir(mode=0o700, parents=True, exist_ok=False)
        episodes_root = root / "episodes"
        episodes_root.mkdir(mode=0o700, exist_ok=False)
        self.root = root
        self._episodes_root = episodes_root

    def write_manifest(self, manifest: RunManifest) -> None:
        if type(manifest) is not RunManifest:
            raise TypeError("_ResultStore.write_manifest requires RunManifest")
        _write_json(
            self.root / "run_manifest.json",
            manifest.to_result_payload(),
            replace_existing=False,
        )

    def create_episode(self, *, run_id: str, episode_id: str) -> Path:
        _require_runtime_id(run_id, "_ResultStore.run_id")
        _require_runtime_id(episode_id, "_ResultStore.episode_id")
        episode_root = self._episodes_root / episode_id
        episode_root.mkdir(mode=0o700, exist_ok=False)
        self.write_artifact_history(
            episode_root,
            run_id=run_id,
            episode_id=episode_id,
            records=(),
            replace_existing=False,
        )
        return episode_root

    def write_round(
        self,
        episode_root: Path,
        *,
        result: RoundResult,
        run_id: str,
        episode_id: str,
        mode: RunMode,
    ) -> None:
        if type(result) is not RoundResult:
            raise TypeError("_ResultStore.write_round requires RoundResult")
        _write_json(
            episode_root / f"round_{result.round_index}.json",
            result.to_result_payload(
                run_id=run_id,
                episode_id=episode_id,
                mode=mode,
            ),
            replace_existing=False,
        )

    def write_artifact_history(
        self,
        episode_root: Path,
        *,
        run_id: str,
        episode_id: str,
        records: tuple[ArtifactHistoryRecord, ...],
        replace_existing: bool = True,
    ) -> None:
        _require_runtime_id(run_id, "_ResultStore.run_id")
        _require_runtime_id(episode_id, "_ResultStore.episode_id")
        if not isinstance(records, tuple) or not all(
            type(record) is ArtifactHistoryRecord for record in records
        ):
            raise TypeError("records must contain exact ArtifactHistoryRecord values")
        if not isinstance(replace_existing, bool):
            raise TypeError("replace_existing must be boolean")
        result: dict[str, object] = {
            "schema_version": 1,
            "run_id": run_id,
            "episode_id": episode_id,
            "artifacts": [record.to_result_payload() for record in records],
        }
        _write_json(
            episode_root / "artifact_history.json",
            result,
            replace_existing=replace_existing,
        )


def _write_json(
    path: Path,
    payload: dict[str, object],
    *,
    replace_existing: bool,
) -> None:
    encoded = (
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    if not replace_existing and path.exists():
        raise FileExistsError(f"result file already exists: {path.name}")

    temporary_path = path.with_name(
        f".{path.name}.{secrets.token_hex(8)}.tmp"
    )
    descriptor = os.open(
        temporary_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        if replace_existing:
            os.replace(temporary_path, path)
        else:
            try:
                os.link(temporary_path, path)
            except FileExistsError:
                raise FileExistsError(
                    f"result file already exists: {path.name}"
                ) from None
            temporary_path.unlink()
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


__all__ = [
    "AppliedArtifactReference",
    "ArtifactHistoryRecord",
    "EpisodeResult",
    "EpisodeStatus",
    "RoundResult",
    "RoundStatus",
    "RunManifest",
    "RunMode",
]
