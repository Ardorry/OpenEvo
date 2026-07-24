"""Fail-closed protocol identity gate for ChemBench4K experiments.

This module deliberately does not load benchmark rows and does not implement an
evolution method.  It records whether the surrounding package has enough
evidence to claim the ``frozen_generalization_v2`` protocol.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from openevo_chembench.config import ExperimentConfig


FROZEN_GENERALIZATION_PROTOCOL_ID = "frozen_generalization_v2"
TASK_LOCAL_ONLINE_RECOVERY_PROTOCOL_ID = "task_local_online_recovery_v1"
LEGACY_SINGLE_TASK_PROTOCOL_ID = "chembench.single_task_multi_artifact.v1"

CHEMBENCH4K_REPOSITORIES: tuple[str, ...] = (
    "AI4Chem/ChemBench4K",
    "opencompass/ChemBench4K",
)
CHEMBENCH4K_CATEGORIES: tuple[str, ...] = (
    "Name_Conversion",
    "Property_Prediction",
    "Mol2caption",
    "Caption2mol",
    "Product_Prediction",
    "Retrosynthesis",
    "Yield_Prediction",
    "Temperature_Prediction",
    "Solvent_Prediction",
)

_COMMIT_HASH = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class ProtocolFindingCode(str, Enum):
    """Closed findings emitted by the v2 launch gate."""

    DATASET_IDENTITY_MISMATCH = "BLOCKED_DATASET_IDENTITY_MISMATCH"
    DATASET_SPLIT_MISMATCH = "BLOCKED_DATASET_SPLIT_MISMATCH"
    DATASET_CATEGORY_MISMATCH = "BLOCKED_DATASET_CATEGORY_MISMATCH"
    DATASET_INTEGRITY_UNVERIFIED = "BLOCKED_DATASET_INTEGRITY_UNVERIFIED"
    DATASET_EQUIVALENCE_UNPROVEN = "BLOCKED_DATASET_EQUIVALENCE_UNPROVEN"
    OFFICIAL_PROTOCOL_INCOMPATIBLE = "BLOCKED_OFFICIAL_PROTOCOL_COMPATIBILITY"
    OPENEVO_METHOD_INTEGRATION_MISSING = "BLOCKED_OPENEVO_METHOD_INTEGRATION"
    ZERO_TOOL_BOUNDARY_UNVERIFIED = "BLOCKED_ZERO_TOOL_BOUNDARY"
    FROZEN_ARTIFACT_MISSING = "BLOCKED_FROZEN_ARTIFACT_MISSING"
    LEGACY_PROTOCOL_ID = "BLOCKED_LEGACY_PROTOCOL_ID"
    VERIFIED_ATTESTATION_MISSING = "BLOCKED_VERIFIED_ATTESTATION_MISSING"


@dataclass(frozen=True, slots=True)
class FrozenProtocolEvidence:
    """Non-secret evidence shape; callers do not become trusted attestations."""

    dataset_repository: str
    dataset_revision: str
    dataset_splits: tuple[str, ...]
    dataset_categories: tuple[str, ...]
    dataset_integrity_verified: bool
    opencompass_equivalence_verified: bool
    protocol_id: str
    official_prompt_renderer_verified: bool
    official_evaluator_verified: bool
    core_dataset_job_contracts_used: bool
    registered_text_memory_method: str | None
    core_artifact_lifecycle_used: bool
    core_context_resolution_used: bool
    executor_zero_tool_boundary_verified: bool
    frozen_artifact_sha256: str | None

    def __post_init__(self) -> None:
        if type(self.dataset_repository) is not str or not self.dataset_repository:
            raise ValueError("dataset_repository must be non-empty text")
        if (
            type(self.dataset_revision) is not str
            or _COMMIT_HASH.fullmatch(self.dataset_revision) is None
        ):
            raise ValueError("dataset_revision must be a lowercase commit hash")
        for value, field_name in (
            (self.dataset_splits, "dataset_splits"),
            (self.dataset_categories, "dataset_categories"),
        ):
            if not isinstance(value, tuple) or not value:
                raise ValueError(f"{field_name} must be a non-empty tuple")
            if not all(type(item) is str and item for item in value):
                raise TypeError(f"{field_name} must contain non-empty strings")
            if len(value) != len(set(value)):
                raise ValueError(f"{field_name} must not contain duplicates")
        if type(self.protocol_id) is not str or not self.protocol_id:
            raise ValueError("protocol_id must be non-empty text")
        for field_name in (
            "dataset_integrity_verified",
            "opencompass_equivalence_verified",
            "official_prompt_renderer_verified",
            "official_evaluator_verified",
            "core_dataset_job_contracts_used",
            "core_artifact_lifecycle_used",
            "core_context_resolution_used",
            "executor_zero_tool_boundary_verified",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise TypeError(f"{field_name} must be boolean")
        if self.registered_text_memory_method is not None and (
            type(self.registered_text_memory_method) is not str
            or not self.registered_text_memory_method
        ):
            raise ValueError("registered_text_memory_method must be non-empty text or None")
        if self.frozen_artifact_sha256 is not None and (
            type(self.frozen_artifact_sha256) is not str
            or _SHA256.fullmatch(self.frozen_artifact_sha256) is None
        ):
            raise ValueError("frozen_artifact_sha256 must be a lowercase SHA-256 digest or None")


@dataclass(frozen=True, slots=True)
class ProtocolGateReceipt:
    """Target-free, deterministic readiness receipt."""

    protocol_id: str
    dataset_repository: str
    dataset_revision: str
    passed: bool
    finding_codes: tuple[ProtocolFindingCode, ...]

    def __post_init__(self) -> None:
        if type(self.protocol_id) is not str or not self.protocol_id:
            raise ValueError("protocol_id must be non-empty text")
        if type(self.dataset_repository) is not str or not self.dataset_repository:
            raise ValueError("dataset_repository must be non-empty text")
        if (
            type(self.dataset_revision) is not str
            or _COMMIT_HASH.fullmatch(self.dataset_revision) is None
        ):
            raise ValueError("dataset_revision must be a lowercase commit hash")
        if type(self.passed) is not bool:
            raise TypeError("passed must be boolean")
        if self.passed:
            raise ValueError(
                "positive v2 receipts are unavailable until a verified attestor exists"
            )
        if not isinstance(self.finding_codes, tuple) or not all(
            type(code) is ProtocolFindingCode for code in self.finding_codes
        ):
            raise TypeError("finding_codes must contain ProtocolFindingCode values")
        if len(self.finding_codes) != len(set(self.finding_codes)):
            raise ValueError("finding_codes must be unique")
        if not self.finding_codes:
            raise ValueError("blocked protocol receipts must contain findings")

    def to_audit_payload(self) -> dict[str, object]:
        """Return a strict public receipt without task, target, or artifact content."""

        return {
            "schema_version": 1,
            "protocol_id": self.protocol_id,
            "dataset_repository": self.dataset_repository,
            "dataset_revision": self.dataset_revision,
            "passed": self.passed,
            "paid_execution_allowed": self.passed,
            "finding_codes": [code.value for code in self.finding_codes],
        }


def assess_frozen_generalization_v2(
    evidence: FrozenProtocolEvidence,
) -> ProtocolGateReceipt:
    """Structurally assess evidence; launchers must use a trusted evidence source."""

    if type(evidence) is not FrozenProtocolEvidence:
        raise TypeError("evidence must be exact FrozenProtocolEvidence")

    findings: list[ProtocolFindingCode] = []
    if evidence.dataset_repository not in CHEMBENCH4K_REPOSITORIES:
        findings.append(ProtocolFindingCode.DATASET_IDENTITY_MISMATCH)
    if (
        evidence.dataset_repository == "opencompass/ChemBench4K"
        and not evidence.opencompass_equivalence_verified
    ):
        findings.append(ProtocolFindingCode.DATASET_EQUIVALENCE_UNPROVEN)
    if set(evidence.dataset_splits) != {"dev", "test"}:
        findings.append(ProtocolFindingCode.DATASET_SPLIT_MISMATCH)
    if evidence.dataset_categories != CHEMBENCH4K_CATEGORIES:
        findings.append(ProtocolFindingCode.DATASET_CATEGORY_MISMATCH)
    if not evidence.dataset_integrity_verified:
        findings.append(ProtocolFindingCode.DATASET_INTEGRITY_UNVERIFIED)
    if evidence.protocol_id != FROZEN_GENERALIZATION_PROTOCOL_ID:
        findings.append(ProtocolFindingCode.LEGACY_PROTOCOL_ID)
    if not (evidence.official_prompt_renderer_verified and evidence.official_evaluator_verified):
        findings.append(ProtocolFindingCode.OFFICIAL_PROTOCOL_INCOMPATIBLE)
    if not (
        evidence.core_dataset_job_contracts_used
        and evidence.registered_text_memory_method is not None
        and evidence.core_artifact_lifecycle_used
        and evidence.core_context_resolution_used
    ):
        findings.append(ProtocolFindingCode.OPENEVO_METHOD_INTEGRATION_MISSING)
    if not evidence.executor_zero_tool_boundary_verified:
        findings.append(ProtocolFindingCode.ZERO_TOOL_BOUNDARY_UNVERIFIED)
    if evidence.frozen_artifact_sha256 is None:
        findings.append(ProtocolFindingCode.FROZEN_ARTIFACT_MISSING)
    # This package currently has no verifier that derives these claims from
    # immutable dataset and Core receipts. Caller-provided evidence can inform
    # an audit, but it can never authorize paid execution.
    findings.append(ProtocolFindingCode.VERIFIED_ATTESTATION_MISSING)

    unique_findings = tuple(dict.fromkeys(findings))
    return ProtocolGateReceipt(
        protocol_id=FROZEN_GENERALIZATION_PROTOCOL_ID,
        dataset_repository=evidence.dataset_repository,
        dataset_revision=evidence.dataset_revision,
        passed=not unique_findings,
        finding_codes=unique_findings,
    )


def current_adapter_frozen_v2_evidence(
    config: ExperimentConfig,
) -> FrozenProtocolEvidence:
    """Describe the current adapter honestly; do not infer unimplemented wiring."""

    if type(config) is not ExperimentConfig:
        raise TypeError("config must be exact ExperimentConfig")
    return FrozenProtocolEvidence(
        dataset_repository=config.dataset.repository,
        dataset_revision=config.chembench_revision,
        dataset_splits=(config.dataset.split,),
        dataset_categories=config.dataset.configurations,
        dataset_integrity_verified=False,
        opencompass_equivalence_verified=False,
        protocol_id=config.protocol_id,
        official_prompt_renderer_verified=False,
        official_evaluator_verified=False,
        core_dataset_job_contracts_used=False,
        registered_text_memory_method=None,
        core_artifact_lifecycle_used=False,
        core_context_resolution_used=False,
        executor_zero_tool_boundary_verified=False,
        frozen_artifact_sha256=None,
    )


def audit_current_adapter_for_frozen_v2(
    config: ExperimentConfig,
) -> ProtocolGateReceipt:
    """Return the current adapter's fail-closed v2 launch receipt."""

    return assess_frozen_generalization_v2(current_adapter_frozen_v2_evidence(config))


__all__ = [
    "CHEMBENCH4K_CATEGORIES",
    "CHEMBENCH4K_REPOSITORIES",
    "FROZEN_GENERALIZATION_PROTOCOL_ID",
    "LEGACY_SINGLE_TASK_PROTOCOL_ID",
    "TASK_LOCAL_ONLINE_RECOVERY_PROTOCOL_ID",
    "FrozenProtocolEvidence",
    "ProtocolFindingCode",
    "ProtocolGateReceipt",
    "assess_frozen_generalization_v2",
    "audit_current_adapter_for_frozen_v2",
    "current_adapter_frozen_v2_evidence",
]
