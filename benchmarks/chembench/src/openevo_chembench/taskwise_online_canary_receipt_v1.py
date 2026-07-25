"""Online-only canary evidence for taskwise Pilot500 authorization.

This gate is intentionally independent from the repeated-session control arm.
It recomputes the current online canary, Core lineage, executor security, and
Pilot500 input bindings from filesystem evidence.  Callers provide path
authorities only; no caller-supplied pass/fail boolean is accepted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any

from openevo.evolution.framework import canonical_digest, load_verified_framework_registry
from openevo_chembench.chembench4k_dataset import ChemBench4KDatasetLoader
from openevo_chembench.source_identity_v2 import sha256_file, verify_source_manifest
from openevo_chembench.taskwise_canary_receipt_v1 import (
    _actual_codex_version,
    _read_regular,
    _recompute_arm_evidence,
    _recompute_core_evidence,
    _workspace_path,
)
from openevo_chembench.taskwise_config_v1 import (
    SOURCE_COMMIT_PLACEHOLDER,
    TaskwiseExperimentConfigV1,
    load_taskwise_config_v1,
)
from openevo_chembench.taskwise_core_evolution_v1 import METHOD_ID
from openevo_chembench.taskwise_sampling_v1 import (
    PILOT500_STREAM_COUNT,
    PILOT500_STREAM_SCOPES,
    PILOT500_TASKS_PER_STREAM,
    pilot500_stream_suite_summary_bytes,
    verify_taskwise_manifests,
)


RECEIPT_SCHEMA = "taskwise_online_canary_mechanism_receipt_v1"
AUTHORITY_SCHEMA = "taskwise_online_canary_pilot_authorization_v1"
EVIDENCE_CLASS = "ONLINE_CANARY_MECHANISM_AND_SECURITY_ONLY"
CONTROL_DISCLAIMER = "NO_CONTROL_ARM"
PERFORMANCE_DISCLAIMER = "NOT_A_PERFORMANCE_COMPARISON"
FULL_AUTHORIZATION = "MISSING"
FULL_EXECUTION_ALLOWED = False
FULL_STATUS = "NOT_STARTED_WAITING_FOR_USER"
ONLINE_CLASSIFICATION = (
    "ONLINE_TASKWISE_EVOLUTION",
    "TEST_TIME_ADAPTATION",
    "ONLINE_ONLY_PILOT500",
    "UNPAIRED_ONLINE_ONLY_NOT_FOR_PAIRED_INFERENCE",
    "ONLINE_CANARY_MECHANISM_AND_SECURITY_ONLY",
    "NO_CONTROL_ARM",
    "NOT_A_PERFORMANCE_COMPARISON",
    "NON_STANDARD_CHEMBENCH4K_PROTOCOL",
    "NOT_A_STANDARD_LEADERBOARD_SCORE",
    "DESCRIPTIVE_ONLINE_ONLY_RESULT",
    "NO_CAUSAL_CONTROL_COMPARISON",
    "NO_PAIRED_PERFORMANCE_CLAIM",
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_RESERVED_FIELDS = frozenset(
    {
        "schema_version",
        "evidence_class",
        "control_disclaimer",
        "performance_disclaimer",
        "classification",
        "created_at_utc",
        "online_only_pilot_allowed",
        "finding_codes",
        "directly_verified_fields",
        "evidence_digest",
    }
)


class OnlineCanaryFindingV1(str, Enum):
    """Closed online-only canary finding vocabulary."""

    SOURCE_INVALID = "SOURCE_INVALID"
    CORE_SOURCE_DIRTY = "CORE_SOURCE_DIRTY"
    DATASET_INVALID = "DATASET_INVALID"
    ONLINE_CONFIG_INVALID = "ONLINE_CONFIG_INVALID"
    CANARY_MANIFEST_INVALID = "CANARY_MANIFEST_INVALID"
    CANARY_GENERATION_INVALID = "CANARY_GENERATION_INVALID"
    CODEX_IDENTITY_INVALID = "CODEX_IDENTITY_INVALID"
    METHOD_REGISTRY_INVALID = "METHOD_REGISTRY_INVALID"
    ONLINE_CANARY_RUN_INVALID = "ONLINE_CANARY_RUN_INVALID"
    ONLINE_CANARY_CORE_INVALID = "ONLINE_CANARY_CORE_INVALID"
    ONLINE_PILOT_BINDING_INVALID = "ONLINE_PILOT_BINDING_INVALID"


@dataclass(frozen=True, slots=True)
class OnlineCanaryReceiptInputsV1:
    """Filesystem authorities read by the online-only receipt gate."""

    repository_root: Path
    package_root: Path
    dataset_root: Path
    source_manifest_path: Path
    framework_lock_path: Path
    online_config_path: Path

    def __post_init__(self) -> None:
        for field_name in self.__dataclass_fields__:
            if not isinstance(getattr(self, field_name), Path):
                raise TypeError(f"{field_name} must be pathlib.Path")


@dataclass(frozen=True, slots=True)
class OnlineCanaryMechanismReceiptV1:
    """Content-free online mechanism evidence and derived Pilot authority."""

    fields: Mapping[str, Any]
    finding_codes: tuple[OnlineCanaryFindingV1, ...]
    created_at_utc: str

    def __post_init__(self) -> None:
        if type(self.fields) is not dict or not all(type(key) is str for key in self.fields):
            raise TypeError("receipt fields must be an exact string-keyed dict")
        if _RESERVED_FIELDS.intersection(self.fields):
            raise ValueError("receipt fields cannot override reserved fields")
        if (
            type(self.finding_codes) is not tuple
            or not all(type(item) is OnlineCanaryFindingV1 for item in self.finding_codes)
            or len(self.finding_codes) != len(set(self.finding_codes))
        ):
            raise TypeError("finding_codes must be unique closed enum values")
        parsed = datetime.fromisoformat(self.created_at_utc)
        if parsed.tzinfo != UTC or parsed.isoformat(timespec="seconds") != self.created_at_utc:
            raise ValueError("created_at_utc must be canonical UTC seconds")
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))

    @property
    def online_only_pilot_allowed(self) -> bool:
        return not self.finding_codes

    @property
    def evidence_digest(self) -> str:
        return _sha256_json(
            {
                "schema_version": RECEIPT_SCHEMA,
                "evidence_class": EVIDENCE_CLASS,
                "control_disclaimer": CONTROL_DISCLAIMER,
                "performance_disclaimer": PERFORMANCE_DISCLAIMER,
                "classification": list(ONLINE_CLASSIFICATION),
                **self.fields,
                "finding_codes": [item.value for item in self.finding_codes],
                "online_only_pilot_allowed": self.online_only_pilot_allowed,
            }
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": RECEIPT_SCHEMA,
            "evidence_class": EVIDENCE_CLASS,
            "control_disclaimer": CONTROL_DISCLAIMER,
            "performance_disclaimer": PERFORMANCE_DISCLAIMER,
            "classification": list(ONLINE_CLASSIFICATION),
            **self.fields,
            "created_at_utc": self.created_at_utc,
            "online_only_pilot_allowed": self.online_only_pilot_allowed,
            "finding_codes": [item.value for item in self.finding_codes],
            "directly_verified_fields": sorted(self.fields),
            "evidence_digest": self.evidence_digest,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.to_payload())


@dataclass(frozen=True, slots=True, init=False)
class OnlineCanaryPilotAuthorizationV1:
    """In-process capability issued only by exact online receipt verification."""

    receipt_sha256: str
    evidence_digest: str
    source_commit: str
    online_canary_generation_id: str
    online_pilot_binding_sha256: str

    def __init__(
        self,
        *,
        receipt_sha256: str,
        evidence_digest: str,
        source_commit: str,
        online_canary_generation_id: str,
        online_pilot_binding_sha256: str,
        _issuer_token: object | None = None,
    ) -> None:
        if not _authorization_token_is_valid(_issuer_token):
            raise TypeError("online Pilot authorization is issued only by the receipt gate")
        for value in (receipt_sha256, evidence_digest, online_pilot_binding_sha256):
            if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
                raise ValueError("authorization digest is invalid")
        _validate_generation_id(online_canary_generation_id)
        if type(source_commit) is not str or _COMMIT_RE.fullmatch(source_commit) is None:
            raise ValueError("authorization source commit is invalid")
        object.__setattr__(self, "receipt_sha256", receipt_sha256)
        object.__setattr__(self, "evidence_digest", evidence_digest)
        object.__setattr__(self, "source_commit", source_commit)
        object.__setattr__(self, "online_canary_generation_id", online_canary_generation_id)
        object.__setattr__(
            self,
            "online_pilot_binding_sha256",
            online_pilot_binding_sha256,
        )


def _make_authorization_issuer() -> tuple[Any, Any]:
    token = object()

    def token_is_valid(candidate: object | None) -> bool:
        return candidate is token

    def issue(**values: str) -> OnlineCanaryPilotAuthorizationV1:
        return OnlineCanaryPilotAuthorizationV1(**values, _issuer_token=token)

    return token_is_valid, issue


_authorization_token_is_valid, _issue_online_canary_authorization_v1 = _make_authorization_issuer()


def default_online_canary_receipt_inputs_v1(
    package_root: Path,
) -> OnlineCanaryReceiptInputsV1:
    package = package_root.resolve()
    repository = package.parents[1]
    workspace = repository.parent
    revision = "f8ad41a980170f4c5d0cc97e57722d06887c8f53"
    return OnlineCanaryReceiptInputsV1(
        repository_root=repository,
        package_root=package,
        dataset_root=workspace / "data" / "chembench4k" / "AI4Chem_ChemBench4K" / revision,
        source_manifest_path=package / "manifests" / "chembench_source_manifest_v2.json",
        framework_lock_path=package / "state" / "v2" / "framework" / "framework-lock.json",
        online_config_path=package / "configs" / "online_canary9_taskwise_online_v1.yaml",
    )


def current_online_canary_generation_id_v1(inputs: OnlineCanaryReceiptInputsV1) -> str:
    """Derive a static online-only canary generation without run outcomes."""

    if type(inputs) is not OnlineCanaryReceiptInputsV1:
        raise TypeError("inputs must be exact OnlineCanaryReceiptInputsV1")
    source_commit = _git(inputs.repository_root, "rev-parse", "HEAD")
    source_manifest_sha256 = verify_source_manifest(
        inputs.package_root,
        inputs.source_manifest_path,
    )
    config = load_taskwise_config_v1(inputs.online_config_path)
    loader = ChemBench4KDatasetLoader(
        snapshot_root=inputs.dataset_root,
        manifest_path=inputs.dataset_root / "chembench4k_dataset_manifest_v2.json",
    )
    manifest = _verify_online_manifest(inputs, config, loader, scope="canary9")
    authority = {
        "source_commit": source_commit,
        "source_manifest_sha256": source_manifest_sha256,
        "dataset_sha256": loader.manifest.combined_sha256,
        "online_config_sha256": config.config_sha256(),
        "online_run_id": config.run_name,
        "canary_public_manifest_sha256": manifest.public_sha256,
        "canary_ordered_uid_sha256": manifest.ordered_uid_sha256,
        "model": config.model,
        "reasoning_effort": config.reasoning_effort,
        "codex_cli_version": config.codex_cli_version,
    }
    return f"online_canary_{_sha256_json(authority)}"


def default_online_canary_receipt_path_v1(
    package_root: Path,
    generation_id: str | None = None,
) -> Path:
    generation = generation_id
    if generation is None:
        generation = current_online_canary_generation_id_v1(
            default_online_canary_receipt_inputs_v1(package_root)
        )
    _validate_generation_id(generation)
    return (
        package_root.resolve()
        / "state"
        / "taskwise_online_v1"
        / "online_canary_receipts"
        / generation
        / "online_canary_mechanism_receipt_v1.json"
    )


def recompute_online_canary_receipt_v1(
    inputs: OnlineCanaryReceiptInputsV1,
) -> OnlineCanaryMechanismReceiptV1:
    """Recompute online-only canary evidence without reading control results."""

    if type(inputs) is not OnlineCanaryReceiptInputsV1:
        raise TypeError("inputs must be exact OnlineCanaryReceiptInputsV1")
    fields: dict[str, Any] = {
        "full4009_authorization": FULL_AUTHORIZATION,
        "full4009_execution_allowed": FULL_EXECUTION_ALLOWED,
        "full4009_status": FULL_STATUS,
    }
    findings: set[OnlineCanaryFindingV1] = set()
    source_commit: str | None = None
    config: TaskwiseExperimentConfigV1 | None = None
    loader: ChemBench4KDatasetLoader | None = None
    manifest: Any = None
    registry: Any = None
    arm_evidence: Any = None

    try:
        source_commit = _git(inputs.repository_root, "rev-parse", "HEAD")
        if _COMMIT_RE.fullmatch(source_commit) is None:
            raise RuntimeError
        source_status = _git(
            inputs.repository_root,
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--",
            "benchmarks/chembench",
        )
        fields["source_commit"] = source_commit
        fields["source_manifest_sha256"] = verify_source_manifest(
            inputs.package_root,
            inputs.source_manifest_path,
        )
        if source_status:
            raise RuntimeError
    except Exception:
        fields.setdefault("source_commit", source_commit)
        fields.setdefault("source_manifest_sha256", None)
        findings.add(OnlineCanaryFindingV1.SOURCE_INVALID)

    try:
        if _git(inputs.repository_root, "status", "--porcelain", "--", "src/openevo"):
            raise RuntimeError
        fields["openevo_core_git_commit"] = source_commit
        fields["openevo_core_source_pristine"] = True
    except Exception:
        fields["openevo_core_source_pristine"] = False
        findings.add(OnlineCanaryFindingV1.CORE_SOURCE_DIRTY)

    try:
        loader = ChemBench4KDatasetLoader(
            snapshot_root=inputs.dataset_root,
            manifest_path=inputs.dataset_root / "chembench4k_dataset_manifest_v2.json",
        )
        if (
            loader.manifest.repository != "AI4Chem/ChemBench4K"
            or loader.manifest.revision != "f8ad41a980170f4c5d0cc97e57722d06887c8f53"
        ):
            raise RuntimeError
        fields.update(
            {
                "dataset_repository": loader.manifest.repository,
                "dataset_revision": loader.manifest.revision,
                "dataset_combined_sha256": loader.manifest.combined_sha256,
                "dataset_manifest_sha256": sha256_file(
                    inputs.dataset_root / "chembench4k_dataset_manifest_v2.json"
                ),
            }
        )
    except Exception:
        loader = None
        fields.update(
            {
                "dataset_repository": None,
                "dataset_revision": None,
                "dataset_combined_sha256": None,
                "dataset_manifest_sha256": None,
            }
        )
        findings.add(OnlineCanaryFindingV1.DATASET_INVALID)

    try:
        config = load_taskwise_config_v1(inputs.online_config_path)
        if (
            config.arm != "online"
            or config.scope != "canary9"
            or config.protocol_id != "taskwise_online_evolution_v1"
            or config.attempts_per_task != 3
            or config.evolution_updates_per_task != 2
            or not config.evolution.enabled
            or config.evolution.method_id != METHOD_ID
            or config.model != "gpt-5.5"
            or config.reasoning_effort != "medium"
            or config.source_commit not in {SOURCE_COMMIT_PLACEHOLDER, source_commit}
        ):
            raise RuntimeError
        fields.update(
            {
                "online_config_sha256": config.config_sha256(),
                "online_run_id": config.run_name,
                "model": config.model,
                "reasoning_effort": config.reasoning_effort,
                "configured_codex_cli_version": config.codex_cli_version,
                "executor_policy_sha256": _sha256_json(config.to_payload()["executor"]),
                "memory_limits_sha256": config.memory_limits.digest,
            }
        )
    except Exception:
        config = None
        fields.update(
            {
                "online_config_sha256": None,
                "online_run_id": None,
                "model": None,
                "reasoning_effort": None,
                "configured_codex_cli_version": None,
                "executor_policy_sha256": None,
                "memory_limits_sha256": None,
            }
        )
        findings.add(OnlineCanaryFindingV1.ONLINE_CONFIG_INVALID)

    try:
        if config is None or loader is None:
            raise RuntimeError
        manifest = _verify_online_manifest(inputs, config, loader, scope="canary9")
        if manifest.item_count != 9:
            raise RuntimeError
        fields.update(
            {
                "canary_public_manifest_sha256": manifest.public_sha256,
                "canary_private_manifest_sha256": manifest.private_sha256,
                "canary_ordered_uid_sha256": manifest.ordered_uid_sha256,
                "canary_task_count": manifest.item_count,
            }
        )
    except Exception:
        manifest = None
        fields.update(
            {
                "canary_public_manifest_sha256": None,
                "canary_private_manifest_sha256": None,
                "canary_ordered_uid_sha256": None,
                "canary_task_count": None,
            }
        )
        findings.add(OnlineCanaryFindingV1.CANARY_MANIFEST_INVALID)

    try:
        generation = current_online_canary_generation_id_v1(inputs)
        fields["online_canary_generation_id"] = generation
    except Exception:
        generation = None
        fields["online_canary_generation_id"] = None
        findings.add(OnlineCanaryFindingV1.CANARY_GENERATION_INVALID)

    try:
        actual_codex_version = _actual_codex_version(inputs.repository_root)
        if config is None or actual_codex_version != config.codex_cli_version:
            raise RuntimeError
        fields["codex_cli_version"] = actual_codex_version
    except Exception:
        fields["codex_cli_version"] = None
        findings.add(OnlineCanaryFindingV1.CODEX_IDENTITY_INVALID)

    try:
        registry = load_verified_framework_registry(inputs.framework_lock_path)
        descriptor = registry.snapshot.methods[METHOD_ID]
        fields.update(
            {
                "framework_lock_sha256": sha256_file(inputs.framework_lock_path),
                "method_id": METHOD_ID,
                "method_descriptor_digest": canonical_digest(descriptor),
                "method_identity_digest": registry.snapshot.identity_digest_for(
                    "method",
                    METHOD_ID,
                ),
            }
        )
    except Exception:
        registry = None
        fields.update(
            {
                "framework_lock_sha256": None,
                "method_id": METHOD_ID,
                "method_descriptor_digest": None,
                "method_identity_digest": None,
            }
        )
        findings.add(OnlineCanaryFindingV1.METHOD_REGISTRY_INVALID)

    try:
        if config is None or loader is None or manifest is None or source_commit is None:
            raise RuntimeError
        output = _workspace_path(inputs.repository_root.parent, config.output_directory)
        state = json.loads(_read_regular(output / "run_state.json", mode=0o644))
        binding = state.get("binding")
        protocol_sha256 = binding.get("protocol_sha256") if type(binding) is dict else None
        arm_evidence = _recompute_arm_evidence(
            inputs=inputs,
            config=config,
            paired=None,
            loader=loader,
            manifest_sha256=manifest.public_sha256,
            source_commit=source_commit,
            protocol_sha256=protocol_sha256,
        )
        fields.update(
            {
                "run_state_sha256": arm_evidence.run_state_sha256,
                "run_binding_sha256": arm_evidence.run_binding_sha256,
                "run_protocol_sha256": protocol_sha256,
                "run_status": arm_evidence.run_status,
                "task_session_count": arm_evidence.completion_count,
                "executor_success_receipt_count": (arm_evidence.executor_success_receipt_count),
                "executor_success_receipts_sha256": (
                    arm_evidence.executor_success_receipts_sha256
                ),
                "task_cleanup_complete_count": arm_evidence.cleanup_complete_count,
                "task_cleanup_residual_root_count": (arm_evidence.cleanup_residual_root_count),
                "security_violation_count": arm_evidence.security_violation_count,
                "context_binding_violation_count": (arm_evidence.context_binding_violation_count),
                "meeting722_contract_receipt_sha256": (
                    arm_evidence.meeting722_contract_receipt_sha256
                ),
                "public_event_chain_sha256": arm_evidence.public_event_chain_sha256,
                "private_evaluation_chain_sha256": (arm_evidence.private_evaluation_chain_sha256),
            }
        )
    except Exception:
        arm_evidence = None
        for key in (
            "run_state_sha256",
            "run_binding_sha256",
            "run_protocol_sha256",
            "run_status",
            "task_session_count",
            "executor_success_receipt_count",
            "executor_success_receipts_sha256",
            "task_cleanup_complete_count",
            "task_cleanup_residual_root_count",
            "security_violation_count",
            "context_binding_violation_count",
            "meeting722_contract_receipt_sha256",
            "public_event_chain_sha256",
            "private_evaluation_chain_sha256",
        ):
            fields[key] = None
        findings.add(OnlineCanaryFindingV1.ONLINE_CANARY_RUN_INVALID)

    try:
        if config is None or registry is None or arm_evidence is None:
            raise RuntimeError
        results, core_fields = _recompute_core_evidence(
            inputs=inputs,
            config=config,
            registry=registry,
            public_rows=arm_evidence.public_rows,
            private_rows=arm_evidence.private_rows,
        )
        if (
            len(results) != 18
            or any(result.job_state != "COMPLETED" for result in results)
            or len({result.job_id for result in results}) != 18
            or len({result.core_artifact_id for result in results}) != 18
            or len({result.context_resolution_digest for result in results}) != 18
            or core_fields.get("online_artifact_chain_violation_count") != 0
            or core_fields.get("online_reflector_cleanup_complete_count") != 18
        ):
            raise RuntimeError
        fields.update(core_fields)
        fields.update(
            {
                "online_update_count": 18,
                "online_core_job_count": 18,
                "online_core_artifact_count": 18,
                "online_context_resolution_count": 18,
                "artifact_validation_failure_count": 0,
                "artifact_chain_violation_count": 0,
                "cross_task_memory_carry_verified": True,
                "reflector_cleanup_residual_root_count": 0,
            }
        )
    except Exception:
        for key in (
            "online_core_update_count",
            "online_core_lineage_sha256",
            "online_core_head_artifact_id",
            "online_core_head_memory_sha256",
            "online_core_checkpoint_sha256",
            "online_core_store_evidence_sha256",
            "online_reflector_receipt_count",
            "online_reflector_cleanup_complete_count",
            "online_reflector_receipts_sha256",
            "online_artifact_chain_violation_count",
            "online_update_count",
            "online_core_job_count",
            "online_core_artifact_count",
            "online_context_resolution_count",
            "artifact_validation_failure_count",
            "artifact_chain_violation_count",
            "cross_task_memory_carry_verified",
            "reflector_cleanup_residual_root_count",
        ):
            fields[key] = None
        findings.add(OnlineCanaryFindingV1.ONLINE_CANARY_CORE_INVALID)

    try:
        if loader is None or generation is None:
            raise RuntimeError
        fields.update(_recompute_online_pilot_binding(inputs, loader, generation))
    except Exception:
        fields.update(
            {
                "online_pilot_binding_sha256": None,
                "online_pilot_suite_summary_sha256": None,
                "online_pilot_ordered_uid_sha256": None,
            }
        )
        findings.add(OnlineCanaryFindingV1.ONLINE_PILOT_BINDING_INVALID)

    return OnlineCanaryMechanismReceiptV1(
        fields=fields,
        finding_codes=tuple(sorted(findings, key=lambda item: item.value)),
        created_at_utc=datetime.now(UTC).isoformat(timespec="seconds"),
    )


def write_online_canary_receipt_v1(
    inputs: OnlineCanaryReceiptInputsV1,
    receipt_path: Path,
) -> OnlineCanaryMechanismReceiptV1:
    """Write one passing receipt exclusively with private permissions."""

    receipt = recompute_online_canary_receipt_v1(inputs)
    if not receipt.online_only_pilot_allowed:
        raise RuntimeError("online canary mechanism findings block online-only Pilot500")
    receipt_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    receipt_path.parent.chmod(0o700)
    descriptor = os.open(
        receipt_path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(receipt.canonical_bytes())
        stream.flush()
        os.fsync(stream.fileno())
    return receipt


def verify_online_canary_receipt_v1(
    inputs: OnlineCanaryReceiptInputsV1,
    receipt_path: Path,
) -> OnlineCanaryPilotAuthorizationV1:
    """Recompute all online evidence before issuing the in-process capability."""

    try:
        metadata = receipt_path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
        ):
            raise OSError
        stored_bytes = receipt_path.read_bytes()
        stored = json.loads(stored_bytes)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("online canary mechanism receipt is unavailable") from exc
    timestamp = stored.get("created_at_utc") if type(stored) is dict else None
    try:
        parsed_timestamp = datetime.fromisoformat(timestamp) if type(timestamp) is str else None
    except ValueError:
        parsed_timestamp = None
    if (
        type(stored) is not dict
        or stored_bytes != _canonical_bytes(stored)
        or parsed_timestamp is None
        or parsed_timestamp.tzinfo != UTC
        or parsed_timestamp.isoformat(timespec="seconds") != timestamp
    ):
        raise RuntimeError("online canary mechanism receipt is invalid")
    current = recompute_online_canary_receipt_v1(inputs)
    expected = current.to_payload()
    expected.pop("created_at_utc")
    stored_without_time = dict(stored)
    stored_without_time.pop("created_at_utc", None)
    if stored_without_time != expected or not current.online_only_pilot_allowed:
        raise RuntimeError("online canary mechanism receipt no longer matches evidence")
    generation = current.fields.get("online_canary_generation_id")
    source_commit = current.fields.get("source_commit")
    pilot_binding = current.fields.get("online_pilot_binding_sha256")
    if (
        type(generation) is not str
        or type(source_commit) is not str
        or type(pilot_binding) is not str
    ):
        raise RuntimeError("online canary authorization fields are unavailable")
    return _issue_online_canary_authorization_v1(
        receipt_sha256=hashlib.sha256(stored_bytes).hexdigest(),
        evidence_digest=current.evidence_digest,
        source_commit=source_commit,
        online_canary_generation_id=generation,
        online_pilot_binding_sha256=pilot_binding,
    )


def require_online_canary_authorization_v1(
    package_root: Path,
) -> OnlineCanaryPilotAuthorizationV1:
    inputs = default_online_canary_receipt_inputs_v1(package_root)
    generation = current_online_canary_generation_id_v1(inputs)
    return verify_online_canary_receipt_v1(
        inputs,
        default_online_canary_receipt_path_v1(package_root, generation),
    )


def _verify_online_manifest(
    inputs: OnlineCanaryReceiptInputsV1,
    config: TaskwiseExperimentConfigV1,
    loader: ChemBench4KDatasetLoader,
    *,
    scope: str,
) -> Any:
    public = _workspace_path(inputs.repository_root.parent, config.task_manifest)
    private = _workspace_path(inputs.repository_root.parent, config.private_task_manifest)
    summary = public.with_name(public.name.replace("_public_manifest.jsonl", "_summary.json"))
    return verify_taskwise_manifests(
        loader,
        scope=scope,
        public_path=public,
        private_path=private,
        summary_path=summary,
    )


def _recompute_online_pilot_binding(
    inputs: OnlineCanaryReceiptInputsV1,
    loader: ChemBench4KDatasetLoader,
    generation_id: str,
) -> dict[str, str]:
    _validate_generation_id(generation_id)
    config_payload: list[dict[str, str]] = []
    manifests = []
    for scope in PILOT500_STREAM_SCOPES:
        config = load_taskwise_config_v1(
            inputs.package_root / "configs" / f"online_{scope}_taskwise_online_v1.yaml"
        )
        if config.arm != "online" or config.scope != scope:
            raise RuntimeError
        manifest = _verify_online_manifest(inputs, config, loader, scope=scope)
        manifests.append(manifest)
        config_payload.append(
            {
                "scope": scope,
                "online_config_sha256": config.config_sha256(),
                "public_manifest_sha256": manifest.public_sha256,
                "private_manifest_sha256": manifest.private_sha256,
                "ordered_uid_sha256": manifest.ordered_uid_sha256,
            }
        )
    expected_summary = pilot500_stream_suite_summary_bytes(
        loader,
        stream_manifests=tuple(manifests),
    )
    summary_path = (
        inputs.package_root / "manifests" / "taskwise_online_v1" / "online_pilot500_summary.json"
    )
    if summary_path.read_bytes() != expected_summary:
        raise RuntimeError
    summary = json.loads(expected_summary)
    if (
        summary.get("stream_count") != PILOT500_STREAM_COUNT
        or summary.get("tasks_per_stream") != PILOT500_TASKS_PER_STREAM
        or summary.get("total_item_count") != 500
        or summary.get("reset_memory_between_streams") is not True
    ):
        raise RuntimeError
    binding = {
        "schema_version": "taskwise_online_only_pilot500_binding_v1",
        "online_canary_generation_id": generation_id,
        "suite_summary_sha256": hashlib.sha256(expected_summary).hexdigest(),
        "ordered_uid_sha256": summary["global_ordered_uid_sha256"],
        "stream_configs": config_payload,
        "classification": [
            "ONLINE_ONLY_PILOT500",
            "UNPAIRED_ONLINE_ONLY_NOT_FOR_PAIRED_INFERENCE",
            "NO_CONTROL_ARM",
            "NO_CAUSAL_CONTROL_COMPARISON",
        ],
    }
    return {
        "online_pilot_binding_sha256": _sha256_json(binding),
        "online_pilot_suite_summary_sha256": binding["suite_summary_sha256"],
        "online_pilot_ordered_uid_sha256": binding["ordered_uid_sha256"],
    }


def _git(repository_root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=repository_root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError
    return completed.stdout.strip()


def _validate_generation_id(value: str) -> str:
    if (
        type(value) is not str
        or not value.startswith("online_canary_")
        or _SHA256_RE.fullmatch(value.removeprefix("online_canary_")) is None
    ):
        raise ValueError("online canary generation id is invalid")
    return value


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


def _sha256_json(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _safe_authorization_payload(
    authorization: OnlineCanaryPilotAuthorizationV1,
) -> dict[str, object]:
    return {
        "schema_version": AUTHORITY_SCHEMA,
        "status": "PASS",
        "classification": list(ONLINE_CLASSIFICATION),
        "online_only_pilot_allowed": True,
        "receipt_sha256": authorization.receipt_sha256,
        "evidence_digest": authorization.evidence_digest,
        "source_commit": authorization.source_commit,
        "online_canary_generation_id": authorization.online_canary_generation_id,
        "online_pilot_binding_sha256": authorization.online_pilot_binding_sha256,
        "full4009_authorization": FULL_AUTHORIZATION,
        "full4009_execution_allowed": FULL_EXECUTION_ALLOWED,
        "full4009_status": FULL_STATUS,
        "model_calls": 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chembench-taskwise-online-canary-receipt-v1")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("freeze")
    commands.add_parser("verify")
    arguments = parser.parse_args(argv)
    command = arguments.command
    package_root = Path(__file__).resolve().parents[2]
    inputs = default_online_canary_receipt_inputs_v1(package_root)
    try:
        generation = current_online_canary_generation_id_v1(inputs)
        receipt_path = default_online_canary_receipt_path_v1(package_root, generation)
        if command == "freeze" and not receipt_path.exists():
            write_online_canary_receipt_v1(inputs, receipt_path)
        authorization = verify_online_canary_receipt_v1(inputs, receipt_path)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "error_type": type(exc).__name__,
                    "model_calls": 0,
                },
                sort_keys=True,
            ),
            file=os.sys.stderr,
        )
        return 2
    print(json.dumps(_safe_authorization_payload(authorization), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "AUTHORITY_SCHEMA",
    "CONTROL_DISCLAIMER",
    "EVIDENCE_CLASS",
    "FULL_AUTHORIZATION",
    "FULL_EXECUTION_ALLOWED",
    "FULL_STATUS",
    "ONLINE_CLASSIFICATION",
    "OnlineCanaryFindingV1",
    "OnlineCanaryMechanismReceiptV1",
    "OnlineCanaryPilotAuthorizationV1",
    "OnlineCanaryReceiptInputsV1",
    "current_online_canary_generation_id_v1",
    "default_online_canary_receipt_inputs_v1",
    "default_online_canary_receipt_path_v1",
    "main",
    "recompute_online_canary_receipt_v1",
    "require_online_canary_authorization_v1",
    "verify_online_canary_receipt_v1",
    "write_online_canary_receipt_v1",
]
