"""Recomputed paired-canary authorization for taskwise paid pilot execution.

The receipt is standalone benchmark evidence, not an OpenEvo product release
attestation.  Callers provide filesystem authorities only.  They cannot supply
``passed`` or other verification booleans, and the paid-pilot decision is
derived exclusively from closed findings recomputed from the frozen inputs and
persisted canary evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
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
from openevo_chembench.reflector_execution_boundary_v2 import (
    ReflectorBoundaryStatusV2,
    ReflectorExecutionReceiptV2,
    TASKWISE_SOURCE_SPLIT,
)
from openevo_chembench.source_identity_v2 import sha256_file, verify_source_manifest
from openevo_chembench.taskwise_config_v1 import (
    TaskwiseExperimentConfigV1,
    load_taskwise_config_v1,
    taskwise_arm_parity_findings,
)
from openevo_chembench.taskwise_core_evolution_v1 import (
    METHOD_ID,
    TaskwiseArtifactLineageReceiptV1,
    TaskwiseCoreEvolutionBridgeV1,
    TaskwiseCoreUpdateResultV1,
)
from openevo_chembench.taskwise_generation_v1 import (
    derive_taskwise_generation_id_v1,
    taskwise_canary_comparison_path_v1,
    taskwise_canary_receipt_path_v1,
    validate_taskwise_generation_id_v1,
)
from openevo_chembench.taskwise_attempt_v1 import ATTEMPT_SCHEMA_V1
from openevo_chembench.taskwise_online_runner_v1 import (
    TaskwiseMemoryPublicAggregateV1,
    TaskwiseMemoryPublicMetricsV1,
    TaskwiseRunConfigV1,
    TaskwiseRunStatusV1,
    _chain_rows,
    _context_binding_from_public_completion,
    _trajectory_from_core_record,
    build_taskwise_run_binding_v1,
    load_private_taskwise_results_v1,
)
from openevo_chembench.taskwise_context_binding_v1 import (
    taskwise_context_binding_receipt_from_public_dict,
)
from openevo_chembench.taskwise_reporting_v1 import (
    TaskwiseCanaryEvidenceV1,
    build_taskwise_canary9_report_v1,
)
from openevo_chembench.taskwise_sampling_v1 import (
    PILOT500_STREAM_COUNT,
    PILOT500_STREAM_SCOPES,
    PILOT500_TASKS_PER_STREAM,
    pilot500_stream_suite_summary_bytes,
    select_taskwise_stream,
    verify_taskwise_manifests,
)
from openevo_chembench.taskwise_trajectory_v1 import (
    ordered_safe_feedback_digest,
    ordered_taskwise_trajectory_digest,
)


RECEIPT_SCHEMA = "taskwise_paired_canary_receipt_v1"
EVIDENCE_CLASS = "STANDALONE_MAINTAINER_BENCHMARK_EVIDENCE"
PRODUCT_DISCLAIMER = "NOT_OPENEVO_PRODUCT_RELEASE_ATTESTATION"
CANARY_CLASSIFICATION = "NON_PERFORMANCE_SAFETY_CANARY"
CANARY_EVIDENCE_SCOPE = "CANARY_MECHANISM_AND_SECURITY_ONLY"
PERFORMANCE_GATE_DISCLAIMER = "NOT_A_PERFORMANCE_GATE"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ZERO_SHA256 = "0" * 64
_RESERVED_FIELDS = frozenset(
    {
        "schema_version",
        "evidence_class",
        "product_release_disclaimer",
        "canary_classification",
        "canary_evidence_scope",
        "performance_gate_disclaimer",
        "created_at_utc",
        "paid_pilot_allowed",
        "finding_codes",
        "directly_verified_fields",
        "evidence_digest",
    }
)


class TaskwiseCanaryFindingV1(str, Enum):
    SOURCE_INVALID = "SOURCE_INVALID"
    CORE_SOURCE_DIRTY = "CORE_SOURCE_DIRTY"
    DATASET_INVALID = "DATASET_INVALID"
    CANARY_CONFIG_INVALID = "CANARY_CONFIG_INVALID"
    CANARY_GENERATION_INVALID = "CANARY_GENERATION_INVALID"
    CANARY_MANIFEST_INVALID = "CANARY_MANIFEST_INVALID"
    PILOT_BINDING_INVALID = "PILOT_BINDING_INVALID"
    CODEX_IDENTITY_INVALID = "CODEX_IDENTITY_INVALID"
    METHOD_REGISTRY_INVALID = "METHOD_REGISTRY_INVALID"
    CANARY_RUN_INVALID = "CANARY_RUN_INVALID"
    CANARY_RUN_BINDING_INVALID = "CANARY_RUN_BINDING_INVALID"
    CANARY_EVENT_CHAIN_INVALID = "CANARY_EVENT_CHAIN_INVALID"
    CANARY_FIXED_BUDGET_INVALID = "CANARY_FIXED_BUDGET_INVALID"
    CANARY_EXECUTOR_AUDIT_INVALID = "CANARY_EXECUTOR_AUDIT_INVALID"
    CONTROL_TREATMENT_INVALID = "CONTROL_TREATMENT_INVALID"
    CORE_PRIVATE_CHECKPOINT_INVALID = "CORE_PRIVATE_CHECKPOINT_INVALID"
    CORE_FAILURE_DIAGNOSTICS_PRESENT = "CORE_FAILURE_DIAGNOSTICS_PRESENT"
    CORE_DATASET_INVALID = "CORE_DATASET_INVALID"
    CORE_JOB_INVALID = "CORE_JOB_INVALID"
    CORE_ARTIFACT_INVALID = "CORE_ARTIFACT_INVALID"
    CORE_CONTEXT_INVALID = "CORE_CONTEXT_INVALID"
    CORE_PUBLIC_PRIVATE_MISMATCH = "CORE_PUBLIC_PRIVATE_MISMATCH"
    REFLECTOR_SECURITY_INVALID = "REFLECTOR_SECURITY_INVALID"
    CANARY_GATE_FAILED = "CANARY_GATE_FAILED"


@dataclass(frozen=True, slots=True)
class TaskwisePairedCanaryReceiptInputsV1:
    """Path authorities read by the receipt gate; no claimed verification."""

    repository_root: Path
    package_root: Path
    dataset_root: Path
    source_manifest_path: Path
    framework_lock_path: Path
    control_config_path: Path
    online_config_path: Path

    def __post_init__(self) -> None:
        for field_name in self.__dataclass_fields__:
            if not isinstance(getattr(self, field_name), Path):
                raise TypeError(f"{field_name} must be pathlib.Path")


@dataclass(frozen=True, slots=True)
class TaskwisePairedCanaryReceiptV1:
    """Content-free paired-canary evidence and its derived pilot decision."""

    fields: Mapping[str, Any]
    finding_codes: tuple[TaskwiseCanaryFindingV1, ...]
    created_at_utc: str

    def __post_init__(self) -> None:
        if type(self.fields) is not dict or not all(type(key) is str for key in self.fields):
            raise TypeError("receipt fields must be an exact string-keyed dict")
        if _RESERVED_FIELDS.intersection(self.fields):
            raise ValueError("receipt fields cannot override reserved fields")
        if (
            type(self.finding_codes) is not tuple
            or not all(type(item) is TaskwiseCanaryFindingV1 for item in self.finding_codes)
            or len(self.finding_codes) != len(set(self.finding_codes))
        ):
            raise TypeError("finding_codes must be unique closed enum values")
        if type(self.created_at_utc) is not str or not self.created_at_utc:
            raise TypeError("created_at_utc must be non-empty text")
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))

    @property
    def paid_pilot_allowed(self) -> bool:
        return not self.finding_codes

    @property
    def evidence_digest(self) -> str:
        return _sha256_json(
            {
                "schema_version": RECEIPT_SCHEMA,
                "evidence_class": EVIDENCE_CLASS,
                "product_release_disclaimer": PRODUCT_DISCLAIMER,
                "canary_classification": CANARY_CLASSIFICATION,
                "canary_evidence_scope": CANARY_EVIDENCE_SCOPE,
                "performance_gate_disclaimer": PERFORMANCE_GATE_DISCLAIMER,
                **self.fields,
                "finding_codes": [item.value for item in self.finding_codes],
                "paid_pilot_allowed": self.paid_pilot_allowed,
            }
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": RECEIPT_SCHEMA,
            "evidence_class": EVIDENCE_CLASS,
            "product_release_disclaimer": PRODUCT_DISCLAIMER,
            "canary_classification": CANARY_CLASSIFICATION,
            "canary_evidence_scope": CANARY_EVIDENCE_SCOPE,
            "performance_gate_disclaimer": PERFORMANCE_GATE_DISCLAIMER,
            **self.fields,
            "created_at_utc": self.created_at_utc,
            "paid_pilot_allowed": self.paid_pilot_allowed,
            "finding_codes": [item.value for item in self.finding_codes],
            "directly_verified_fields": sorted(self.fields),
            "evidence_digest": self.evidence_digest,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.to_payload())


@dataclass(frozen=True, slots=True, init=False)
class TaskwisePilotAuthorizationV1:
    """In-process capability issued only after exact receipt recomputation."""

    receipt_sha256: str
    evidence_digest: str
    source_commit: str
    pilot_binding_sha256: str
    paired_canary_generation_id: str

    def __init__(
        self,
        *,
        receipt_sha256: str,
        evidence_digest: str,
        source_commit: str,
        pilot_binding_sha256: str,
        paired_canary_generation_id: str,
        _issuer_token: object | None = None,
    ) -> None:
        if not _authorization_token_is_valid(_issuer_token):
            raise TypeError("TaskwisePilotAuthorizationV1 is issued only by the receipt gate")
        for value in (receipt_sha256, evidence_digest, pilot_binding_sha256):
            if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
                raise ValueError("authorization digests must be SHA-256")
        if type(source_commit) is not str or re.fullmatch(r"[0-9a-f]{40}", source_commit) is None:
            raise ValueError("authorization source_commit must be a Git commit")
        validate_taskwise_generation_id_v1(paired_canary_generation_id)
        object.__setattr__(self, "receipt_sha256", receipt_sha256)
        object.__setattr__(self, "evidence_digest", evidence_digest)
        object.__setattr__(self, "source_commit", source_commit)
        object.__setattr__(self, "pilot_binding_sha256", pilot_binding_sha256)
        object.__setattr__(
            self,
            "paired_canary_generation_id",
            paired_canary_generation_id,
        )


def _make_authorization_issuer() -> tuple[Any, Any]:
    token = object()

    def token_is_valid(candidate: object | None) -> bool:
        return candidate is token

    def issue(**values: str) -> TaskwisePilotAuthorizationV1:
        return TaskwisePilotAuthorizationV1(**values, _issuer_token=token)

    return token_is_valid, issue


_authorization_token_is_valid, _issue_taskwise_pilot_authorization_v1 = (
    _make_authorization_issuer()
)


def default_taskwise_canary_receipt_inputs_v1(
    package_root: Path,
) -> TaskwisePairedCanaryReceiptInputsV1:
    """Return the fixed package authorities used by the paid taskwise CLI."""

    package = package_root.resolve()
    repository = package.parents[1]
    workspace = repository.parent
    revision = "f8ad41a980170f4c5d0cc97e57722d06887c8f53"
    dataset_root = workspace / "data" / "chembench4k" / "AI4Chem_ChemBench4K" / revision
    return TaskwisePairedCanaryReceiptInputsV1(
        repository_root=repository,
        package_root=package,
        dataset_root=dataset_root,
        source_manifest_path=package / "manifests" / "chembench_source_manifest_v2.json",
        framework_lock_path=package / "state" / "v2" / "framework" / "framework-lock.json",
        control_config_path=package / "configs" / "control_canary9_taskwise_online_v1.yaml",
        online_config_path=package / "configs" / "online_canary9_taskwise_online_v1.yaml",
    )


def current_taskwise_canary_generation_id_v1(
    inputs: TaskwisePairedCanaryReceiptInputsV1,
) -> str:
    """Derive the current paired generation without consulting run outcomes."""

    if type(inputs) is not TaskwisePairedCanaryReceiptInputsV1:
        raise TypeError("inputs must be exact TaskwisePairedCanaryReceiptInputsV1")
    source_commit = _git(inputs.repository_root, "rev-parse", "HEAD")
    if re.fullmatch(r"[0-9a-f]{40}", source_commit) is None:
        raise RuntimeError("current source commit is invalid")
    source_manifest_sha256 = verify_source_manifest(
        inputs.package_root,
        inputs.source_manifest_path,
    )
    configs = {
        "control": load_taskwise_config_v1(inputs.control_config_path),
        "online": load_taskwise_config_v1(inputs.online_config_path),
    }
    if (
        configs["control"].scope != "canary9"
        or configs["online"].scope != "canary9"
        or taskwise_arm_parity_findings(configs["control"], configs["online"])
    ):
        raise RuntimeError("paired canary configs are invalid")
    public = _workspace_path(
        inputs.repository_root.parent,
        configs["control"].task_manifest,
    )
    summary = public.with_name(public.name.replace("_public_manifest.jsonl", "_summary.json"))
    authority = {
        "source_commit": source_commit,
        "source_manifest_sha256": source_manifest_sha256,
        "dataset_manifest_sha256": sha256_file(
            inputs.dataset_root / "chembench4k_dataset_manifest_v2.json"
        ),
        "control_config_sha256": configs["control"].config_sha256(),
        "online_config_sha256": configs["online"].config_sha256(),
        "control_run_id": configs["control"].run_name,
        "online_run_id": configs["online"].run_name,
        "canary_public_manifest_sha256": sha256_file(public),
        "canary_summary_sha256": sha256_file(summary),
    }
    return derive_taskwise_generation_id_v1(authority)


def default_taskwise_canary_receipt_path_v1(
    package_root: Path,
    generation_id: str | None = None,
) -> Path:
    package = package_root.resolve()
    generation = generation_id
    if generation is None:
        generation = current_taskwise_canary_generation_id_v1(
            default_taskwise_canary_receipt_inputs_v1(package)
        )
    return taskwise_canary_receipt_path_v1(package, generation)


def recompute_taskwise_paired_canary_receipt_v1(
    inputs: TaskwisePairedCanaryReceiptInputsV1,
) -> TaskwisePairedCanaryReceiptV1:
    """Recompute current paired-canary and pilot-binding evidence fail closed."""

    if type(inputs) is not TaskwisePairedCanaryReceiptInputsV1:
        raise TypeError("inputs must be exact TaskwisePairedCanaryReceiptInputsV1")
    findings: set[TaskwiseCanaryFindingV1] = set()
    fields: dict[str, Any] = {
        "protocol_id": "taskwise_online_evolution_v1",
        "control_protocol_id": "repeated_session_control_v1",
    }

    head: str | None = None
    try:
        head = _git(inputs.repository_root, "rev-parse", "HEAD")
        if re.fullmatch(r"[0-9a-f]{40}", head) is None:
            raise RuntimeError
        fields["source_commit"] = head
        fields["chembench_source_manifest_sha256"] = verify_source_manifest(
            inputs.package_root,
            inputs.source_manifest_path,
        )
        package_status = _git(
            inputs.repository_root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            "benchmarks/chembench",
        )
        if package_status:
            raise RuntimeError
    except Exception:
        fields.setdefault("source_commit", head)
        fields["chembench_source_manifest_sha256"] = None
        findings.add(TaskwiseCanaryFindingV1.SOURCE_INVALID)
    try:
        if _git(
            inputs.repository_root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            "src/openevo",
        ):
            findings.add(TaskwiseCanaryFindingV1.CORE_SOURCE_DIRTY)
    except Exception:
        findings.add(TaskwiseCanaryFindingV1.CORE_SOURCE_DIRTY)

    loader: ChemBench4KDatasetLoader | None = None
    try:
        loader = ChemBench4KDatasetLoader(snapshot_root=inputs.dataset_root)
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
        fields.update(
            {
                "dataset_repository": None,
                "dataset_revision": None,
                "dataset_combined_sha256": None,
                "dataset_manifest_sha256": None,
            }
        )
        findings.add(TaskwiseCanaryFindingV1.DATASET_INVALID)

    configs: dict[str, TaskwiseExperimentConfigV1] = {}
    canary_manifest = None
    try:
        configs = {
            "control": load_taskwise_config_v1(inputs.control_config_path),
            "online": load_taskwise_config_v1(inputs.online_config_path),
        }
        if (
            configs["control"].scope != "canary9"
            or configs["online"].scope != "canary9"
            or taskwise_arm_parity_findings(configs["control"], configs["online"])
        ):
            raise RuntimeError
        fields["control_config_sha256"] = configs["control"].config_sha256()
        fields["online_config_sha256"] = configs["online"].config_sha256()
        fields["canary_protocol_sha256"] = _paired_protocol_sha256(configs)
        fields["model"] = configs["control"].model
        fields["reasoning_effort"] = configs["control"].reasoning_effort
        fields["executor_policy_sha256"] = _executor_policy_sha256(configs["control"])
        fields["control_run_id"] = configs["control"].run_name
        fields["online_run_id"] = configs["online"].run_name
        if loader is None:
            raise RuntimeError
        public = _workspace_path(inputs.repository_root.parent, configs["control"].task_manifest)
        private = _workspace_path(
            inputs.repository_root.parent,
            configs["control"].private_task_manifest,
        )
        summary = public.with_name(public.name.replace("_public_manifest.jsonl", "_summary.json"))
        canary_manifest = verify_taskwise_manifests(
            loader,
            scope="canary9",
            public_path=public,
            private_path=private,
            summary_path=summary,
        )
        fields.update(
            {
                "canary_public_manifest_sha256": canary_manifest.public_sha256,
                "canary_private_manifest_sha256": canary_manifest.private_sha256,
                "canary_summary_sha256": canary_manifest.summary_sha256,
                "canary_ordered_uid_sha256": canary_manifest.ordered_uid_sha256,
                "canary_item_count": canary_manifest.item_count,
            }
        )
    except Exception:
        for key in (
            "control_config_sha256",
            "online_config_sha256",
            "canary_protocol_sha256",
            "model",
            "reasoning_effort",
            "executor_policy_sha256",
            "control_run_id",
            "online_run_id",
        ):
            fields.setdefault(key, None)
        if not configs:
            findings.add(TaskwiseCanaryFindingV1.CANARY_CONFIG_INVALID)
        else:
            findings.add(TaskwiseCanaryFindingV1.CANARY_MANIFEST_INVALID)
        canary_manifest = None

    paired_generation_id: str | None = None
    try:
        paired_generation_id = current_taskwise_canary_generation_id_v1(inputs)
        fields["paired_canary_generation_id"] = paired_generation_id
    except Exception:
        fields["paired_canary_generation_id"] = None
        findings.add(TaskwiseCanaryFindingV1.CANARY_GENERATION_INVALID)

    try:
        fields["codex_cli_version"] = _actual_codex_version(inputs.repository_root)
        if (
            not configs
            or fields["codex_cli_version"] != configs["control"].codex_cli_version
            or configs["online"].codex_cli_version != configs["control"].codex_cli_version
        ):
            raise RuntimeError
    except Exception:
        fields["codex_cli_version"] = None
        findings.add(TaskwiseCanaryFindingV1.CODEX_IDENTITY_INVALID)

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
        findings.add(TaskwiseCanaryFindingV1.METHOD_REGISTRY_INVALID)

    try:
        if paired_generation_id is None:
            raise RuntimeError
        pilot_binding = _recompute_pilot_binding(
            inputs,
            loader,
            generation_id=paired_generation_id,
        )
        fields.update(pilot_binding)
    except Exception:
        fields["pilot_binding_sha256"] = None
        fields["pilot_suite_summary_sha256"] = None
        fields["pilot_ordered_uid_sha256"] = None
        findings.add(TaskwiseCanaryFindingV1.PILOT_BINDING_INVALID)

    run_evidence: dict[str, _ArmEvidence] = {}
    if configs and loader is not None and canary_manifest is not None and head is not None:
        for arm in ("control", "online"):
            try:
                run_evidence[arm] = _recompute_arm_evidence(
                    inputs=inputs,
                    config=configs[arm],
                    paired=configs,
                    loader=loader,
                    manifest_sha256=canary_manifest.public_sha256,
                    source_commit=head,
                )
            except _ArmEvidenceError as exc:
                findings.update(exc.findings)
            except Exception:
                findings.add(TaskwiseCanaryFindingV1.CANARY_RUN_INVALID)
    else:
        findings.add(TaskwiseCanaryFindingV1.CANARY_RUN_INVALID)

    for arm in ("control", "online"):
        evidence = run_evidence.get(arm)
        prefix = f"{arm}_"
        if evidence is None:
            fields.update(
                {
                    prefix + "run_state_sha256": None,
                    prefix + "run_binding_sha256": None,
                    prefix + "run_status": None,
                    prefix + "public_event_chain_sha256": None,
                    prefix + "private_evaluation_chain_sha256": None,
                    prefix + "completion_count": None,
                    prefix + "core_job_count": None,
                    prefix + "core_artifact_count": None,
                    prefix + "security_violation_count": None,
                    prefix + "context_binding_violation_count": None,
                    prefix + "artifact_chain_violation_count": None,
                    prefix + "cleanup_complete_count": None,
                    prefix + "cleanup_residual_root_count": None,
                    prefix + "executor_success_receipt_count": None,
                    prefix + "executor_success_receipts_sha256": None,
                }
            )
        else:
            fields.update(
                {
                    prefix + "run_state_sha256": evidence.run_state_sha256,
                    prefix + "run_binding_sha256": evidence.run_binding_sha256,
                    prefix + "run_status": evidence.run_status,
                    prefix + "public_event_chain_sha256": (evidence.public_event_chain_sha256),
                    prefix + "private_evaluation_chain_sha256": (
                        evidence.private_evaluation_chain_sha256
                    ),
                    prefix + "completion_count": evidence.completion_count,
                    prefix + "core_job_count": evidence.core_job_count,
                    prefix + "core_artifact_count": evidence.core_artifact_count,
                    prefix + "security_violation_count": (evidence.security_violation_count),
                    prefix + "context_binding_violation_count": (
                        evidence.context_binding_violation_count
                    ),
                    prefix + "artifact_chain_violation_count": (0 if arm == "control" else None),
                    prefix + "cleanup_complete_count": (evidence.cleanup_complete_count),
                    prefix + "cleanup_residual_root_count": (evidence.cleanup_residual_root_count),
                    prefix + "executor_success_receipt_count": (
                        evidence.executor_success_receipt_count
                    ),
                    prefix + "executor_success_receipts_sha256": (
                        evidence.executor_success_receipts_sha256
                    ),
                }
            )

    core_results: tuple[TaskwiseCoreUpdateResultV1, ...] = ()
    online_evidence = run_evidence.get("online")
    if online_evidence is not None and registry is not None and configs:
        try:
            core_results, core_fields = _recompute_core_evidence(
                inputs=inputs,
                config=configs["online"],
                registry=registry,
                public_rows=online_evidence.public_rows,
                private_rows=online_evidence.private_rows,
            )
            fields.update(core_fields)
        except _CoreEvidenceError as exc:
            findings.update(exc.findings)
        except Exception:
            findings.add(TaskwiseCanaryFindingV1.CORE_PRIVATE_CHECKPOINT_INVALID)
    else:
        findings.add(TaskwiseCanaryFindingV1.CORE_PRIVATE_CHECKPOINT_INVALID)
    if not core_results:
        fields.update(
            {
                "online_core_update_count": None,
                "online_core_lineage_sha256": None,
                "online_core_head_artifact_id": None,
                "online_core_head_memory_sha256": None,
                "online_core_checkpoint_sha256": None,
                "online_core_store_evidence_sha256": None,
                "online_reflector_receipt_count": None,
                "online_reflector_cleanup_complete_count": None,
                "online_reflector_receipts_sha256": None,
            }
        )
    else:
        fields["online_artifact_chain_violation_count"] = 0

    if set(run_evidence) == {"control", "online"}:
        try:
            report = build_taskwise_canary9_report_v1(
                control=load_private_taskwise_results_v1(run_evidence["control"].output_directory),
                online=load_private_taskwise_results_v1(run_evidence["online"].output_directory),
                evidence=TaskwiseCanaryEvidenceV1(
                    control_completion_count=run_evidence["control"].completion_count,
                    online_completion_count=run_evidence["online"].completion_count,
                    control_core_job_count=run_evidence["control"].core_job_count,
                    control_core_artifact_count=(run_evidence["control"].core_artifact_count),
                    online_core_job_count=run_evidence["online"].core_job_count,
                    online_core_artifact_count=run_evidence["online"].core_artifact_count,
                    control_context_binding_violations=(
                        run_evidence["control"].context_binding_violation_count
                    ),
                    online_context_binding_violations=(
                        run_evidence["online"].context_binding_violation_count
                    ),
                    control_security_violations=0,
                    online_security_violations=0,
                    online_artifact_validation_failures=0,
                ),
            )
            gate = report["canary9_gate"]
            expected_report = _canonical_bytes(report)
            if paired_generation_id is None:
                raise ValueError("paired canary generation is unavailable")
            report_path = taskwise_canary_comparison_path_v1(
                inputs.repository_root,
                paired_generation_id,
            )
            persisted_report = _read_regular(report_path, mode=0o600)
            if persisted_report != expected_report:
                raise ValueError("persisted canary report does not match evidence")
            fields["canary_private_report_sha256"] = hashlib.sha256(expected_report).hexdigest()
            fields["canary_gate_sha256"] = _sha256_json(gate)
            if gate.get("passed") is not True or gate.get("finding_codes") != []:
                findings.add(TaskwiseCanaryFindingV1.CANARY_GATE_FAILED)
        except Exception:
            fields["canary_private_report_sha256"] = None
            fields["canary_gate_sha256"] = None
            findings.add(TaskwiseCanaryFindingV1.CANARY_GATE_FAILED)
    else:
        fields["canary_private_report_sha256"] = None
        fields["canary_gate_sha256"] = None
        findings.add(TaskwiseCanaryFindingV1.CANARY_GATE_FAILED)

    return TaskwisePairedCanaryReceiptV1(
        fields=fields,
        finding_codes=tuple(sorted(findings, key=lambda item: item.value)),
        created_at_utc=datetime.now(UTC).isoformat(timespec="seconds"),
    )


def write_taskwise_paired_canary_receipt_v1(
    inputs: TaskwisePairedCanaryReceiptInputsV1,
    output_path: Path,
) -> TaskwisePairedCanaryReceiptV1:
    """Create one immutable receipt; existing evidence is never overwritten."""

    receipt = recompute_taskwise_paired_canary_receipt_v1(inputs)
    if not receipt.paid_pilot_allowed:
        raise RuntimeError("taskwise paired canary findings block receipt freeze")
    output_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    output_path.parent.chmod(0o700)
    descriptor = os.open(
        output_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(receipt.canonical_bytes())
        stream.flush()
        os.fsync(stream.fileno())
    return receipt


def verify_taskwise_paired_canary_receipt_v1(
    inputs: TaskwisePairedCanaryReceiptInputsV1,
    receipt_path: Path,
) -> TaskwisePilotAuthorizationV1:
    """Read once, recompute every authority, then issue a private capability."""

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
        raise RuntimeError("taskwise paired canary receipt is unavailable") from exc
    if type(stored) is not dict:
        raise RuntimeError("taskwise paired canary receipt is invalid")
    current = recompute_taskwise_paired_canary_receipt_v1(inputs)
    expected = current.to_payload()
    expected.pop("created_at_utc", None)
    stored_without_time = dict(stored)
    stored_without_time.pop("created_at_utc", None)
    if stored_without_time != expected:
        raise RuntimeError("taskwise paired canary receipt no longer matches evidence")
    if not current.paid_pilot_allowed:
        raise RuntimeError("taskwise paid pilot is blocked by paired canary findings")
    source_commit = current.fields.get("source_commit")
    pilot_binding = current.fields.get("pilot_binding_sha256")
    generation_id = current.fields.get("paired_canary_generation_id")
    if (
        type(source_commit) is not str
        or type(pilot_binding) is not str
        or type(generation_id) is not str
    ):
        raise RuntimeError("taskwise paired canary authorization fields are unavailable")
    return _issue_taskwise_pilot_authorization_v1(
        receipt_sha256=hashlib.sha256(stored_bytes).hexdigest(),
        evidence_digest=current.evidence_digest,
        source_commit=source_commit,
        pilot_binding_sha256=pilot_binding,
        paired_canary_generation_id=generation_id,
    )


@dataclass(frozen=True, slots=True)
class _ArmEvidence:
    output_directory: Path
    state: dict[str, Any]
    public_rows: tuple[dict[str, Any], ...]
    private_rows: tuple[dict[str, Any], ...]
    run_state_sha256: str
    run_binding_sha256: str
    run_status: str
    public_event_chain_sha256: str
    private_evaluation_chain_sha256: str
    completion_count: int
    core_job_count: int
    core_artifact_count: int
    context_binding_violation_count: int
    security_violation_count: int
    cleanup_complete_count: int
    cleanup_residual_root_count: int
    executor_success_receipt_count: int
    executor_success_receipts_sha256: str


class _ArmEvidenceError(RuntimeError):
    def __init__(self, *findings: TaskwiseCanaryFindingV1) -> None:
        self.findings = frozenset(findings)
        super().__init__("taskwise arm evidence invalid")


class _CoreEvidenceError(RuntimeError):
    def __init__(self, *findings: TaskwiseCanaryFindingV1) -> None:
        self.findings = frozenset(findings)
        super().__init__("taskwise Core evidence invalid")


def _recompute_arm_evidence(
    *,
    inputs: TaskwisePairedCanaryReceiptInputsV1,
    config: TaskwiseExperimentConfigV1,
    paired: dict[str, TaskwiseExperimentConfigV1],
    loader: ChemBench4KDatasetLoader,
    manifest_sha256: str,
    source_commit: str,
) -> _ArmEvidence:
    output = _workspace_path(inputs.repository_root.parent, config.output_directory)
    state_path = output / "run_state.json"
    state_raw = _read_regular(state_path, mode=0o644)
    state = json.loads(state_raw)
    if type(state) is not dict or state_raw != _canonical_bytes(state):
        raise _ArmEvidenceError(TaskwiseCanaryFindingV1.CANARY_RUN_INVALID)
    public_rows = tuple(_read_jsonl(output / "public" / "events.jsonl", mode=0o644))
    private_rows = tuple(_read_jsonl(output / "private" / "evaluations.jsonl", mode=0o600))
    failure_rows = _read_jsonl(output / "private" / "failures.jsonl", mode=0o600)
    if failure_rows:
        raise _ArmEvidenceError(TaskwiseCanaryFindingV1.CANARY_RUN_INVALID)

    tasks, _allocation, _seed = select_taskwise_stream(loader, scope="canary9")
    from openevo_chembench.chembench4k_models import CHEMBENCH4K_CATEGORIES
    from openevo_chembench.chembench4k_prompt import render_official_five_shot_prompt
    from openevo_chembench.taskwise_online_runner_v1 import (
        TaskwiseEpisodeV1,
    )

    dev = {
        category: loader.load_category(category, split="dev")
        for category in CHEMBENCH4K_CATEGORIES
    }
    episodes = tuple(
        TaskwiseEpisodeV1(
            task=task,
            prompt=render_official_five_shot_prompt(
                task.to_public(),
                category_dev=dev[task.category],
            ),
        )
        for task in tasks
    )
    run_config = TaskwiseRunConfigV1(
        arm=config.arm,
        run_id=config.run_name,
        output_directory=output,
        protocol_sha256=_paired_protocol_sha256(paired),
        source_commit=source_commit,
        dataset_sha256=loader.manifest.combined_sha256,
        task_manifest_sha256=manifest_sha256,
        model_identity_sha256=_model_identity_sha256(config),
        executor_policy_sha256=_executor_policy_sha256(config),
        memory_limits=config.memory_limits,
    )
    expected_binding = build_taskwise_run_binding_v1(run_config, episodes)
    if state.get("binding") != expected_binding:
        raise _ArmEvidenceError(TaskwiseCanaryFindingV1.CANARY_RUN_BINDING_INVALID)

    expected_updates = 0 if config.arm == "control" else 18
    if (
        state.get("schema_version") != "taskwise_online_run_state_v1"
        or state.get("arm") != config.arm
        or state.get("status") != TaskwiseRunStatusV1.COMPLETED.value
        or state.get("episode_state") != "TASK_FINALIZED"
        or state.get("task_ordinal") != 9
        or state.get("planned_tasks") != 9
        or state.get("completed_tasks") != 9
        or state.get("completion_count") != 27
        or state.get("session_attempt_count") != 27
        or state.get("update_count") != expected_updates
        or state.get("core_job_count") != expected_updates
        or state.get("core_artifact_count") != expected_updates
        or state.get("context_binding_violation_count") != 0
        or state.get("pending_invocation") is not None
        or state.get("resume_allowed") is not False
        or state.get("failure") is not None
        or len(state.get("issued_session_ids", [])) != 27
        or len(set(state.get("issued_session_ids", []))) != 27
        or len(private_rows) != 27
    ):
        raise _ArmEvidenceError(TaskwiseCanaryFindingV1.CANARY_FIXED_BUDGET_INVALID)
    if _chain_rows(list(public_rows)) != state.get("public_event_chain_sha256") or _chain_rows(
        list(private_rows)
    ) != state.get("private_evaluation_chain_sha256"):
        raise _ArmEvidenceError(TaskwiseCanaryFindingV1.CANARY_EVENT_CHAIN_INVALID)

    completion_rows = [row for row in public_rows if row.get("kind") == "completion"]
    update_rows = [row for row in public_rows if row.get("kind") == "core_update"]
    if len(completion_rows) != 27 or len(update_rows) != expected_updates:
        raise _ArmEvidenceError(TaskwiseCanaryFindingV1.CANARY_FIXED_BUDGET_INVALID)
    expected_sequence: list[tuple[str, int, int]] = []
    for task_index in range(9):
        expected_sequence.append(("completion", task_index, 0))
        if config.arm == "online":
            expected_sequence.append(("core_update", task_index, 1))
        expected_sequence.append(("completion", task_index, 1))
        if config.arm == "online":
            expected_sequence.append(("core_update", task_index, 2))
        expected_sequence.append(("completion", task_index, 2))
    observed = [
        (
            row.get("kind"),
            row.get("task_ordinal"),
            row.get("round_index") if row.get("kind") == "completion" else row.get("update_index"),
        )
        for row in public_rows
    ]
    if observed != expected_sequence:
        raise _ArmEvidenceError(TaskwiseCanaryFindingV1.CANARY_EVENT_CHAIN_INVALID)

    private_by_position = {
        (row.get("task_ordinal"), row.get("round_index")): row for row in private_rows
    }
    if len(private_by_position) != 27:
        raise _ArmEvidenceError(TaskwiseCanaryFindingV1.CANARY_EVENT_CHAIN_INVALID)
    for row in completion_rows:
        try:
            receipt = taskwise_context_binding_receipt_from_public_dict(row.get("context_binding"))
            expected_context = _context_binding_from_public_completion(row)
            private = private_by_position[(row["task_ordinal"], row["round_index"])]
        except Exception as exc:
            raise _ArmEvidenceError(TaskwiseCanaryFindingV1.CANARY_EVENT_CHAIN_INVALID) from exc
        if (
            not receipt.passed
            or receipt.expected != expected_context
            or receipt.actual != expected_context
            or private.get("context_binding") != row.get("context_binding")
            or private.get("memory") != row.get("memory")
            or private.get("official_prediction") != row.get("official_prediction")
            or row.get("session_id") != receipt.session_id
        ):
            raise _ArmEvidenceError(TaskwiseCanaryFindingV1.CANARY_EVENT_CHAIN_INVALID)
        metadata = row.get("runtime_metadata")
        if (
            type(metadata) is not dict
            or metadata.get("model") != config.model
            or metadata.get("codex_cli_version") != config.codex_cli_version
            or metadata.get("harness") != "codex_cli"
            or metadata.get("execution_backend") != "local_codex_cli"
        ):
            raise _ArmEvidenceError(TaskwiseCanaryFindingV1.CODEX_IDENTITY_INVALID)

    if config.arm == "control":
        control_core_state = (
            inputs.package_root / "state" / "taskwise_online_v1" / config.scope / config.run_name
        )
        if (
            update_rows
            or state.get("registered_core_job_ids") != []
            or state.get("registered_core_artifact_ids") != []
            or state.get("carry_memory") is not None
            or state.get("active_memory") is not None
            or state.get("predecessor_artifact_ref") is not None
            or any(row.get("memory") is not None for row in completion_rows)
            or control_core_state.exists()
            or control_core_state.is_symlink()
        ):
            raise _ArmEvidenceError(TaskwiseCanaryFindingV1.CONTROL_TREATMENT_INVALID)
    else:
        aggregate = TaskwiseMemoryPublicAggregateV1.from_dict(state.get("memory_aggregate"))
        if (
            aggregate.approved_artifact_count != 18
            or state.get("registered_core_job_ids")
            != [row.get("core_job_id") for row in update_rows]
            or state.get("registered_core_artifact_ids")
            != [row.get("output_memory", {}).get("core_artifact_id") for row in update_rows]
        ):
            raise _ArmEvidenceError(TaskwiseCanaryFindingV1.CORE_PUBLIC_PRIVATE_MISMATCH)
        recomputed = TaskwiseMemoryPublicAggregateV1.empty(config.memory_limits)
        for row in update_rows:
            recomputed = recomputed.include(
                TaskwiseMemoryPublicMetricsV1.from_dict(row.get("memory_metrics"))
            )
        if recomputed != aggregate:
            raise _ArmEvidenceError(TaskwiseCanaryFindingV1.CORE_PUBLIC_PRIVATE_MISMATCH)

    executor_root = (
        inputs.package_root
        / "state"
        / "taskwise_online_v1"
        / "private_executor_events"
        / config.scope
        / config.run_name
        / config.arm
    )
    try:
        from openevo_chembench.local_codex_executor import (
            load_taskwise_success_receipts_v1,
        )

        expected_session_ids = tuple(str(row["session_id"]) for row in completion_rows)
        executor_receipts = load_taskwise_success_receipts_v1(
            executor_root,
            expected_session_ids=expected_session_ids,
        )
        by_session = {item.session_id: item for item in executor_receipts}
        if len(by_session) != 27:
            raise ValueError
        for row in completion_rows:
            item = by_session[str(row["session_id"])]
            private = private_by_position[(row["task_ordinal"], row["round_index"])]
            transcript = row.get("transcript_reference")
            if (
                item.run_id != config.run_name
                or item.task_uid != private.get("task_uid")
                or item.task_index != row.get("task_ordinal")
                or item.round_index != row.get("round_index")
                or item.tool_event_count != 0
                or item.event_count <= 0
                or item.completion_observed is not True
                or item.process_return_code != 0
                or item.process_signal is not None
                or item.cleanup_status != "COMPLETE"
                or item.residual_root_count != 0
                or item.codex_cli_version != config.codex_cli_version
                or item.model != config.model
                or item.executor_policy_sha256 != _executor_policy_sha256(config)
                or transcript != f"local-codex-jsonl:sha256:{item.event_stream_sha256}"
            ):
                raise ValueError
        executor_digest = _sha256_json([item.to_payload() for item in executor_receipts])
    except Exception as exc:
        raise _ArmEvidenceError(TaskwiseCanaryFindingV1.CANARY_EXECUTOR_AUDIT_INVALID) from exc

    return _ArmEvidence(
        output_directory=output,
        state=state,
        public_rows=public_rows,
        private_rows=private_rows,
        run_state_sha256=hashlib.sha256(state_raw).hexdigest(),
        run_binding_sha256=str(expected_binding["binding_sha256"]),
        run_status=TaskwiseRunStatusV1.COMPLETED.value,
        public_event_chain_sha256=str(state["public_event_chain_sha256"]),
        private_evaluation_chain_sha256=str(state["private_evaluation_chain_sha256"]),
        completion_count=27,
        core_job_count=expected_updates,
        core_artifact_count=expected_updates,
        context_binding_violation_count=0,
        security_violation_count=0,
        cleanup_complete_count=27,
        cleanup_residual_root_count=sum(item.residual_root_count for item in executor_receipts),
        executor_success_receipt_count=27,
        executor_success_receipts_sha256=executor_digest,
    )


def _recompute_core_evidence(
    *,
    inputs: TaskwisePairedCanaryReceiptInputsV1,
    config: TaskwiseExperimentConfigV1,
    registry: Any,
    public_rows: tuple[dict[str, Any], ...],
    private_rows: tuple[dict[str, Any], ...],
) -> tuple[tuple[TaskwiseCoreUpdateResultV1, ...], dict[str, Any]]:
    state_root = (
        inputs.package_root / "state" / "taskwise_online_v1" / config.scope / config.run_name
    )
    failure_root = state_root / "private_core_failure_diagnostics"
    try:
        if failure_root.exists() or failure_root.is_symlink():
            metadata = failure_root.lstat()
            if (
                failure_root.is_symlink()
                or not stat.S_ISDIR(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o700
                or any(failure_root.iterdir())
            ):
                raise ValueError
    except OSError as exc:
        raise _CoreEvidenceError(TaskwiseCanaryFindingV1.CORE_FAILURE_DIAGNOSTICS_PRESENT) from exc
    except ValueError as exc:
        raise _CoreEvidenceError(TaskwiseCanaryFindingV1.CORE_FAILURE_DIAGNOSTICS_PRESENT) from exc
    checkpoint_path = state_root / "private_lineage_checkpoints.jsonl"
    checkpoint_sha256 = sha256_file(checkpoint_path)
    rows = _read_jsonl(checkpoint_path, mode=0o600)
    if len(rows) != 18:
        raise _CoreEvidenceError(TaskwiseCanaryFindingV1.CORE_PRIVATE_CHECKPOINT_INVALID)
    results: list[TaskwiseCoreUpdateResultV1] = []
    forbidden_by_result: list[tuple[str, ...]] = []
    for ordinal, row in enumerate(rows, start=1):
        if (
            set(row)
            != {
                "schema_version",
                "result",
                "required_lineage",
                "required_lineage_sha256",
                "validator_forbidden_literals",
            }
            or row.get("schema_version") != "taskwise_core_private_checkpoint_v1"
        ):
            raise _CoreEvidenceError(TaskwiseCanaryFindingV1.CORE_PRIVATE_CHECKPOINT_INVALID)
        result = TaskwiseCoreUpdateResultV1.model_validate(row["result"])
        lineage = TaskwiseArtifactLineageReceiptV1.model_validate(row["required_lineage"])
        forbidden = tuple(row["validator_forbidden_literals"])
        if (
            result.global_update_ordinal != ordinal
            or lineage != result.required_lineage_receipt()
            or row["required_lineage_sha256"] != lineage.digest
        ):
            raise _CoreEvidenceError(TaskwiseCanaryFindingV1.CORE_PRIVATE_CHECKPOINT_INVALID)
        results.append(result)
        forbidden_by_result.append(forbidden)

    bridge = TaskwiseCoreEvolutionBridgeV1(
        db_path=state_root / "evolution.sqlite3",
        artifact_root=state_root / "artifacts",
        executable_registry=registry,
        reflector_boundary_factory=None,
        checkpoint_path=checkpoint_path,
        memory_limits=config.memory_limits,
    )
    try:
        for result, forbidden in zip(results, forbidden_by_result, strict=True):
            bridge.verify_update_result(
                result,
                validator_forbidden_literals=forbidden,
            )
        head = bridge.current_head()
        if head != results[-1]:
            raise _CoreEvidenceError(TaskwiseCanaryFindingV1.CORE_CONTEXT_INVALID)
    except _CoreEvidenceError:
        raise
    except Exception as exc:
        raise _CoreEvidenceError(
            TaskwiseCanaryFindingV1.CORE_JOB_INVALID,
            TaskwiseCanaryFindingV1.CORE_ARTIFACT_INVALID,
            TaskwiseCanaryFindingV1.CORE_CONTEXT_INVALID,
        ) from exc
    finally:
        bridge.close()

    updates = [row for row in public_rows if row.get("kind") == "core_update"]
    if len(updates) != len(results):
        raise _CoreEvidenceError(TaskwiseCanaryFindingV1.CORE_PUBLIC_PRIVATE_MISMATCH)
    private_by_position = {
        (row.get("task_ordinal"), row.get("round_index")): row for row in private_rows
    }
    prior: TaskwiseCoreUpdateResultV1 | None = None
    for result, event in zip(results, updates, strict=True):
        trajectories = tuple(
            _trajectory_from_core_record(
                private_by_position[(result.task_index, round_index)]["trajectory"]
            )
            for round_index in range(result.update_index)
        )
        output_memory = event.get("output_memory")
        if (
            result.predecessor != (None if prior is None else prior.predecessor_identity())
            or result.trajectory_ids != tuple(item.trajectory_id for item in trajectories)
            or result.trajectory_digest != ordered_taskwise_trajectory_digest(trajectories)
            or result.safe_feedback_digest != ordered_safe_feedback_digest(trajectories)
            or event.get("task_ordinal") != result.task_index
            or event.get("update_index") != result.update_index
            or event.get("core_job_id") != result.job_id
            or event.get("core_job_state") != "COMPLETED"
            or event.get("core_context_id") != result.core_context_id
            or event.get("validation_receipt_sha256") != result.validation_receipt_sha256
            or type(output_memory) is not dict
            or output_memory.get("core_artifact_id") != result.core_artifact_id
            or output_memory.get("artifact_payload_sha256") != result.artifact_payload_sha256
            or output_memory.get("context_resolution_digest") != result.context_resolution_digest
            or output_memory.get("resolved_memory_sha256") != result.resolved_memory_sha256
        ):
            raise _CoreEvidenceError(TaskwiseCanaryFindingV1.CORE_PUBLIC_PRIVATE_MISMATCH)
        prior = result

    core_store_evidence_sha256 = _verify_core_dataset_and_plan_rows(
        state_root,
        tuple(results),
    )
    reflector_digest = _verify_reflector_receipts(
        state_root / "private_reflector_events",
        tuple(results),
    )
    lineage_digest = _sha256_json(
        [
            {
                "global_update_ordinal": result.global_update_ordinal,
                "required_lineage_sha256": result.required_lineage_receipt().digest,
                "job_id": result.job_id,
                "core_artifact_id": result.core_artifact_id,
                "context_resolution_digest": result.context_resolution_digest,
                "resolved_memory_sha256": result.resolved_memory_sha256,
            }
            for result in results
        ]
    )
    return tuple(results), {
        "online_core_update_count": len(results),
        "online_core_lineage_sha256": lineage_digest,
        "online_core_head_artifact_id": results[-1].core_artifact_id,
        "online_core_head_memory_sha256": results[-1].resolved_memory_sha256,
        "online_core_checkpoint_sha256": checkpoint_sha256,
        "online_core_store_evidence_sha256": core_store_evidence_sha256,
        "online_reflector_receipt_count": len(results),
        "online_reflector_cleanup_complete_count": len(results),
        "online_reflector_receipts_sha256": reflector_digest,
        "online_artifact_chain_violation_count": 0,
    }


def _verify_core_dataset_and_plan_rows(
    state_root: Path,
    results: tuple[TaskwiseCoreUpdateResultV1, ...],
) -> str:
    database = state_root / "evolution.sqlite3"
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    evidence: list[dict[str, Any]] = []
    try:
        for result in results:
            dataset = connection.execute(
                "SELECT state, manifest_path, event_count, trace_count, artifact_id "
                "FROM datasets WHERE dataset_id = ?",
                (result.dataset_id,),
            ).fetchone()
            plan = connection.execute(
                "SELECT plan_digest FROM evolution_plans WHERE plan_id = ?",
                (result.plan_id,),
            ).fetchone()
            job = connection.execute(
                "SELECT input_artifact_ids_json FROM jobs WHERE job_id = ?",
                (result.job_id,),
            ).fetchone()
            expected_inputs = [
                result.dataset_artifact_id,
                *([] if result.predecessor is None else [result.predecessor.core_artifact_id]),
            ]
            if (
                dataset is None
                or dataset["state"] != "active"
                or dataset["event_count"] != result.update_index
                or dataset["trace_count"] != result.update_index
                or dataset["artifact_id"] != result.dataset_artifact_id
                or plan is None
                or plan["plan_digest"] != result.plan_digest
                or job is None
                or json.loads(job["input_artifact_ids_json"]) != expected_inputs
            ):
                raise _CoreEvidenceError(
                    TaskwiseCanaryFindingV1.CORE_DATASET_INVALID,
                    TaskwiseCanaryFindingV1.CORE_JOB_INVALID,
                )
            manifest_path = Path(str(dataset["manifest_path"]))
            if (
                not _is_within(manifest_path.resolve(), (state_root / "artifacts").resolve())
                or sha256_file(manifest_path) != result.dataset_manifest_sha256
            ):
                raise _CoreEvidenceError(TaskwiseCanaryFindingV1.CORE_DATASET_INVALID)
            evidence.append(
                {
                    "dataset_id": result.dataset_id,
                    "dataset_artifact_id": result.dataset_artifact_id,
                    "dataset_manifest_sha256": result.dataset_manifest_sha256,
                    "plan_id": result.plan_id,
                    "plan_digest": result.plan_digest,
                    "job_id": result.job_id,
                    "input_artifact_ids": expected_inputs,
                }
            )
    finally:
        connection.close()
    return _sha256_json(evidence)


def _verify_reflector_receipts(
    audit_root: Path,
    results: tuple[TaskwiseCoreUpdateResultV1, ...],
) -> str:
    if not audit_root.is_dir() or audit_root.is_symlink():
        raise _CoreEvidenceError(TaskwiseCanaryFindingV1.REFLECTOR_SECURITY_INVALID)
    receipts: dict[str, dict[str, Any]] = {}
    for path in sorted(audit_root.glob("*/receipt.json")):
        raw = _read_regular(path, mode=0o600)
        payload = json.loads(raw)
        receipt = ReflectorExecutionReceiptV2.from_payload(payload)
        event_path = audit_root / receipt.private_event_reference
        event_raw = _read_regular(event_path, mode=0o600)
        if (
            payload.get("schema_version") != "chembench4k_reflector_execution_receipt_v3"
            or receipt.status is not ReflectorBoundaryStatusV2.COMPLETED
            or receipt.mechanism != "bubblewrap"
            or not receipt.wrapper_invoked
            or receipt.event_counts
            or not receipt.cleanup_complete
            or receipt.retry_allowed
            or receipt.resume_allowed
            or receipt.replacement_completion_allowed
            or receipt.codex_returncode != 0
            or receipt.stderr_tail_codes
            or receipt.source_split != TASKWISE_SOURCE_SPLIT
            or receipt.protocol_id != "taskwise_online_evolution_v1"
            or receipt.last_message_sha256 is None
            or hashlib.sha256(event_raw).hexdigest() != receipt.event_stream_sha256
            or receipt.digest in receipts
        ):
            raise _CoreEvidenceError(TaskwiseCanaryFindingV1.REFLECTOR_SECURITY_INVALID)
        receipts[receipt.digest] = payload
    expected = {result.reflector_execution_receipt_sha256: result for result in results}
    if None in expected or set(receipts) != set(expected):
        raise _CoreEvidenceError(TaskwiseCanaryFindingV1.REFLECTOR_SECURITY_INVALID)
    for digest, result in expected.items():
        receipt = ReflectorExecutionReceiptV2.from_payload(receipts[digest])
        if (
            receipt.record_count != result.update_index
            or receipt.ordered_records_sha256 != result.reflector_input_digest
            or receipt.event_stream_sha256 != result.reflector_event_stream_sha256
        ):
            raise _CoreEvidenceError(TaskwiseCanaryFindingV1.REFLECTOR_SECURITY_INVALID)
    return _sha256_json([receipts[key] for key in sorted(receipts)])


def _recompute_pilot_binding(
    inputs: TaskwisePairedCanaryReceiptInputsV1,
    loader: ChemBench4KDatasetLoader | None,
    *,
    generation_id: str,
) -> dict[str, Any]:
    if loader is None:
        raise RuntimeError
    validate_taskwise_generation_id_v1(generation_id)
    config_root = inputs.package_root / "configs"
    manifests = []
    config_payload: list[dict[str, str]] = []
    for scope in PILOT500_STREAM_SCOPES:
        paired = {
            arm: load_taskwise_config_v1(config_root / f"{arm}_{scope}_taskwise_online_v1.yaml")
            for arm in ("control", "online")
        }
        if taskwise_arm_parity_findings(paired["control"], paired["online"]):
            raise RuntimeError
        control = paired["control"]
        public = _workspace_path(inputs.repository_root.parent, control.task_manifest)
        private = _workspace_path(
            inputs.repository_root.parent,
            control.private_task_manifest,
        )
        summary = public.with_name(public.name.replace("_public_manifest.jsonl", "_summary.json"))
        manifest = verify_taskwise_manifests(
            loader,
            scope=scope,
            public_path=public,
            private_path=private,
            summary_path=summary,
        )
        manifests.append(manifest)
        config_payload.append(
            {
                "scope": scope,
                "control_config_sha256": paired["control"].config_sha256(),
                "online_config_sha256": paired["online"].config_sha256(),
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
        "schema_version": "taskwise_pilot500_binding_v1",
        "generation_id": generation_id,
        "attempt_namespace_schema": ATTEMPT_SCHEMA_V1,
        "suite_summary_sha256": hashlib.sha256(expected_summary).hexdigest(),
        "ordered_uid_sha256": summary["global_ordered_uid_sha256"],
        "stream_configs": config_payload,
    }
    return {
        "pilot_binding_sha256": _sha256_json(binding),
        "pilot_suite_summary_sha256": binding["suite_summary_sha256"],
        "pilot_ordered_uid_sha256": binding["ordered_uid_sha256"],
    }


def _model_identity_sha256(config: TaskwiseExperimentConfigV1) -> str:
    return _sha256_json(
        {
            "model": config.model,
            "reasoning_effort": config.reasoning_effort,
            "codex_cli_version": config.codex_cli_version,
            "prompt_renderer_id": config.prompt_renderer_id,
            "parser_id": config.parser_id,
            "evaluator_id": config.evaluator_id,
        }
    )


def _executor_policy_sha256(config: TaskwiseExperimentConfigV1) -> str:
    return _sha256_json(config.to_payload()["executor"])


def _paired_protocol_sha256(
    paired: dict[str, TaskwiseExperimentConfigV1],
) -> str:
    return _sha256_json(
        {
            "schema_version": "taskwise_paired_protocol_binding_v1",
            "control_config_sha256": paired["control"].config_sha256(),
            "online_config_sha256": paired["online"].config_sha256(),
            "protocol_labels": [
                "ONLINE_TASKWISE_EVOLUTION",
                "TEST_TIME_ADAPTATION",
                "NON_STANDARD_CHEMBENCH4K_PROTOCOL",
                "NOT_A_STANDARD_LEADERBOARD_SCORE",
            ],
        }
    )


def _actual_codex_version(repository_root: Path) -> str:
    completed = subprocess.run(
        ("codex", "--version"),
        cwd=repository_root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
        timeout=15,
        env={"PATH": os.environ.get("PATH", "")},
    )
    if completed.returncode != 0:
        raise RuntimeError
    match = re.fullmatch(r"codex-cli ([0-9A-Za-z.+-]+)\s*", completed.stdout)
    if match is None:
        raise RuntimeError
    return match.group(1)


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


def _workspace_path(workspace_root: Path, value: str) -> Path:
    candidate = (workspace_root / value).resolve()
    if not _is_within(candidate, workspace_root.resolve()):
        raise ValueError("path escapes workspace")
    return candidate


def _read_regular(path: Path, *, mode: int) -> bytes:
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != mode
        or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
    ):
        raise OSError("evidence file mode is invalid")
    return path.read_bytes()


def _read_jsonl(path: Path, *, mode: int) -> list[dict[str, Any]]:
    raw = _read_regular(path, mode=mode)
    if raw and not raw.endswith(b"\n"):
        raise ValueError("JSONL evidence is incomplete")
    rows: list[dict[str, Any]] = []
    for line in raw.splitlines(keepends=True):
        value = json.loads(line)
        if type(value) is not dict or line != _canonical_bytes(value):
            raise ValueError("JSONL evidence is not canonical")
        rows.append(value)
    return rows


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


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


__all__ = [
    "CANARY_CLASSIFICATION",
    "CANARY_EVIDENCE_SCOPE",
    "EVIDENCE_CLASS",
    "PERFORMANCE_GATE_DISCLAIMER",
    "PRODUCT_DISCLAIMER",
    "RECEIPT_SCHEMA",
    "TaskwiseCanaryFindingV1",
    "TaskwisePairedCanaryReceiptInputsV1",
    "TaskwisePairedCanaryReceiptV1",
    "TaskwisePilotAuthorizationV1",
    "current_taskwise_canary_generation_id_v1",
    "default_taskwise_canary_receipt_inputs_v1",
    "default_taskwise_canary_receipt_path_v1",
    "recompute_taskwise_paired_canary_receipt_v1",
    "verify_taskwise_paired_canary_receipt_v1",
    "write_taskwise_paired_canary_receipt_v1",
]
