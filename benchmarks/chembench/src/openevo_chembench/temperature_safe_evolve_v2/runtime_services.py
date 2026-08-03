"""Independent V2 namespace over the audited durable Rollout/Gateway service path."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from openevo_chembench.temperature_full_evolve_v1 import runtime_services as _base
from openevo_chembench.temperature_safe_evolve_v2.formal_runtime import (
    FORMAL_FRAMEWORK_LOCK_RELATIVE,
    FORMAL_RUNTIME_PYTHON_RELATIVE,
    load_temperature_safe_formal_runtime_v2,
)

SERVICE_ROOT_RELATIVE = "state/chembench_temperature_safe_evolve_v2/runtime_services"
_SERVICE_RUN_ID = re.compile(r"stv3-temperature-safe-services-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8,16}\Z")

CompletionRootIdentityV1 = _base.CompletionRootIdentityV1
DurableNoCompletionEvidenceV1 = _base.DurableNoCompletionEvidenceV1
PersistedCompletionAuditV1 = _base.PersistedCompletionAuditV1
PersistedRolloutResultAuditV1 = _base.PersistedRolloutResultAuditV1
TemperatureGatewayContainerIdentityV1 = _base.TemperatureGatewayContainerIdentityV1
TemperatureRuntimeServicesError = _base.TemperatureRuntimeServicesError
TemperatureRuntimeServicesIdentityV1 = _base.TemperatureRuntimeServicesIdentityV1


def _configure() -> None:
    _base.SERVICE_ROOT_RELATIVE = SERVICE_ROOT_RELATIVE
    _base.RUNTIME_PYTHON_RELATIVE = FORMAL_RUNTIME_PYTHON_RELATIVE
    _base.FRAMEWORK_LOCK_RELATIVE = FORMAL_FRAMEWORK_LOCK_RELATIVE
    _base._RUN_ID_RE = _SERVICE_RUN_ID
    _base._GATEWAY_PROTOCOL_LABEL = "temperature_safe_evolve_v2"
    _base.load_temperature_formal_runtime_v1 = load_temperature_safe_formal_runtime_v2


def start_temperature_safe_runtime_services_v2(
    *, repository_root: Path, service_run_id: str
) -> TemperatureRuntimeServicesIdentityV1:
    _configure()
    return _base.start_temperature_runtime_services_v1(
        repository_root=repository_root, service_run_id=service_run_id
    )


def load_temperature_safe_runtime_services_v2(
    *, repository_root: Path
) -> TemperatureRuntimeServicesIdentityV1:
    _configure()
    return _base.load_temperature_runtime_services_v1(repository_root=repository_root)


def load_temperature_safe_runtime_evidence_v2(
    *, repository_root: Path, service_run_id: str
) -> TemperatureRuntimeServicesIdentityV1:
    """Load one immutable V2 service receipt without requiring live processes."""

    _configure()
    return _base.load_temperature_runtime_evidence_v1(
        repository_root=repository_root,
        service_run_id=service_run_id,
    )


def fold_runtime_service_run_id_v2(*, campaign_run_id: str, fold_id: str) -> str:
    """Derive the preregistered, distinct service namespace for one fold."""

    match = re.fullmatch(
        r"stv3-temperature-safe-evolve-v2-(20[0-9]{6}T[0-9]{6}Z)",
        campaign_run_id,
    )
    if match is None or fold_id not in {"R0", "R1", "R2", "R3"}:
        raise TemperatureRuntimeServicesError("TEMPERATURE_SAFE_FOLD_SERVICE_ID_INPUT_INVALID")
    suffix = hashlib.sha256(
        f"temperature-safe-evolve-v2|{campaign_run_id}|{fold_id}|runtime-services".encode()
    ).hexdigest()[:12]
    service_run_id = f"stv3-temperature-safe-services-{match.group(1)}-{suffix}"
    if _SERVICE_RUN_ID.fullmatch(service_run_id) is None:
        raise TemperatureRuntimeServicesError("TEMPERATURE_SAFE_FOLD_SERVICE_ID_INVALID")
    return service_run_id


def stop_temperature_safe_runtime_services_v2(*, repository_root: Path) -> dict[str, object]:
    _configure()
    return _base.stop_temperature_runtime_services_v1(repository_root=repository_root)


def temperature_safe_runtime_services_status_v2(*, repository_root: Path) -> dict[str, object]:
    _configure()
    return _base.temperature_runtime_services_status_v1(repository_root=repository_root)


def audit_persisted_rollout_result_v2(
    *, repository_root: Path, service_run_id: str, task_id: str
) -> PersistedRolloutResultAuditV1:
    _configure()
    return _base.audit_persisted_rollout_result_v1(
        repository_root=repository_root, service_run_id=service_run_id, task_id=task_id
    )


def audit_durable_no_completion_v2(
    *, repository_root: Path, service_run_id: str, task_id: str
) -> DurableNoCompletionEvidenceV1:
    _configure()
    return _base.audit_durable_no_completion_v1(
        repository_root=repository_root, service_run_id=service_run_id, task_id=task_id
    )


def audit_empty_temperature_safe_runtime_services_v2(
    *, repository_root: Path
) -> dict[str, object]:
    _configure()
    return _base.audit_empty_temperature_runtime_services_v1(repository_root=repository_root)


__all__ = [
    "SERVICE_ROOT_RELATIVE",
    "CompletionRootIdentityV1",
    "DurableNoCompletionEvidenceV1",
    "PersistedCompletionAuditV1",
    "PersistedRolloutResultAuditV1",
    "TemperatureGatewayContainerIdentityV1",
    "TemperatureRuntimeServicesError",
    "TemperatureRuntimeServicesIdentityV1",
    "audit_durable_no_completion_v2",
    "audit_empty_temperature_safe_runtime_services_v2",
    "audit_persisted_rollout_result_v2",
    "fold_runtime_service_run_id_v2",
    "load_temperature_safe_runtime_evidence_v2",
    "load_temperature_safe_runtime_services_v2",
    "start_temperature_safe_runtime_services_v2",
    "stop_temperature_safe_runtime_services_v2",
    "temperature_safe_runtime_services_status_v2",
]
