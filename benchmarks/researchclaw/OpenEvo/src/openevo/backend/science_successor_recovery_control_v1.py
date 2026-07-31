"""Private Core-control API for append-only science successor recovery.

The routes are deliberately excluded from the public OpenAPI document.  They
are installed on the authenticated Core v2 application, so the existing Core
control bearer middleware authenticates every request before the coordinator
may read source authority or persist a target intent.
"""

from __future__ import annotations

import json
from typing import Literal, Protocol

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from openevo.backend.science_successor_recovery_v1 import (
    RecoveryExecutionIdentityV1,
    ScienceSuccessorRecoveryCoordinatorV1,
    ScienceSuccessorRecoveryCreateRequestV1,
    ScienceSuccessorRecoveryReadinessErrorV1,
    ScienceSuccessorRecoveryProjectSeedAuthorityV1,
    ScienceSuccessorRecoveryProjectSeedRequestV1,
    ScienceSuccessorRecoverySnapshotV1,
    ScienceSuccessorRecoverySourceResolveRequestV1,
    ScienceSuccessorRecoverySourceResponseV1,
    ScienceSuccessorRecoveryTargetOperationV1,
    ScienceSuccessorRecoveryV1Error,
)
from openevo.runtime.managed_reflector_mount import (
    ManagedReflectorCredentialMountReadiness,
    ManagedReflectorRuntimeReadiness,
)

_PATH = "/v2/internal/science-successor-recoveries"
_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"


class ManagedReflectorReadinessResponseV1(BaseModel):
    """Core bearer-protected current worker identity for readiness publishing."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["openevo.managed_reflector_readiness.v1"] = (
        "openevo.managed_reflector_readiness.v1"
    )
    execution_identity: RecoveryExecutionIdentityV1
    managed_reflector_credential_mount: ManagedReflectorCredentialMountReadiness
    managed_reflector_runtime: ManagedReflectorRuntimeReadiness
    codex_cli_started: Literal[True]
    model_started: Literal[False]


class ManagedReflectorReadinessProviderV1(Protocol):
    def read_managed_reflector_readiness(
        self,
    ) -> ManagedReflectorReadinessResponseV1: ...


class ScienceSuccessorRecoverySourceResolverV1(Protocol):
    def resolve_exhausted_successor_authority(
        self,
        request: ScienceSuccessorRecoverySourceResolveRequestV1,
    ) -> ScienceSuccessorRecoverySourceResponseV1: ...


class ScienceSuccessorRecoveryProjectSeederV1(Protocol):
    def seed_project(
        self,
        request: ScienceSuccessorRecoveryProjectSeedRequestV1,
    ) -> ScienceSuccessorRecoveryProjectSeedAuthorityV1: ...


class ScienceSuccessorRecoveryTargetRunRequestV1(BaseModel):
    """Explicit authorization for exactly one recovery target operation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    science_successor_recovery_target_run_contract_version: str = Field(
        default="1",
        pattern=r"^1$",
    )
    idempotency_key: str = Field(pattern=_ID_PATTERN)


def install_core_science_successor_recovery_endpoint(
    app: FastAPI,
    coordinator: ScienceSuccessorRecoveryCoordinatorV1,
    *,
    source_resolver: ScienceSuccessorRecoverySourceResolverV1 | None = None,
    project_seeder: ScienceSuccessorRecoveryProjectSeederV1 | None = None,
) -> None:
    """Install bearer-authenticated, append-only recovery operations."""

    if not hasattr(app.state, "core_control_v2_provider"):
        raise RuntimeError("science successor recovery requires authenticated Core v2")
    if getattr(app.state, "core_science_successor_recovery_installed", False):
        raise RuntimeError("science successor recovery endpoint is already installed")
    app.state.core_science_successor_recovery_installed = True

    if source_resolver is not None:
        @app.post(
            _PATH + "/sources/resolve",
            response_model=ScienceSuccessorRecoverySourceResponseV1,
            include_in_schema=False,
        )
        async def resolve_recovery_source(
            payload: dict[str, object],
        ) -> ScienceSuccessorRecoverySourceResponseV1:
            try:
                # FastAPI decodes a JSON array to a Python ``list`` before
                # validating endpoint annotations.  The recovery contracts
                # are intentionally strict and use tuples for closed ordered
                # inventories, so validate from canonical JSON mode just as
                # the create route below does.  This preserves the strict
                # contract while accepting its standards-compliant JSON
                # representation; it does not weaken tuple or extra-field
                # validation for Python callers.
                request = ScienceSuccessorRecoverySourceResolveRequestV1.model_validate_json(
                    json.dumps(
                        payload,
                        ensure_ascii=True,
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
                return source_resolver.resolve_exhausted_successor_authority(request)
            except (TypeError, ValueError, ValidationError) as exc:
                raise HTTPException(
                    status_code=422,
                    detail="invalid recovery source resolution request",
                ) from exc
            except ScienceSuccessorRecoveryV1Error as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        _PATH,
        response_model=ScienceSuccessorRecoverySnapshotV1,
        include_in_schema=False,
    )
    async def create_recovery(
        payload: dict[str, object],
    ) -> ScienceSuccessorRecoverySnapshotV1:
        try:
            request = ScienceSuccessorRecoveryCreateRequestV1.model_validate_json(
                json.dumps(
                    payload,
                    ensure_ascii=True,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            return coordinator.create_recovery(request)
        except (TypeError, ValueError, ValidationError) as exc:
            raise HTTPException(status_code=422, detail="invalid recovery request") from exc
        except ScienceSuccessorRecoveryV1Error as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    if project_seeder is not None:
        @app.post(
            _PATH + "/{recovery_id}/seed-project",
            response_model=ScienceSuccessorRecoveryProjectSeedAuthorityV1,
            include_in_schema=False,
        )
        async def seed_recovery_project(
            recovery_id: str,
            payload: dict[str, object],
        ) -> ScienceSuccessorRecoveryProjectSeedAuthorityV1:
            try:
                request = ScienceSuccessorRecoveryProjectSeedRequestV1.model_validate_json(
                    json.dumps(
                        payload,
                        ensure_ascii=True,
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
                if request.recovery_id != recovery_id:
                    raise ValueError("recovery seed path identity differs from payload")
                return project_seeder.seed_project(request)
            except (TypeError, ValueError, ValidationError) as exc:
                raise HTTPException(
                    status_code=422,
                    detail="invalid recovery project seed request",
                ) from exc
            except ScienceSuccessorRecoveryV1Error as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get(
        _PATH + "/{recovery_id}/targets/{target_id}",
        response_model=ScienceSuccessorRecoveryTargetOperationV1,
        include_in_schema=False,
    )
    async def get_target_operation(
        recovery_id: str,
        target_id: str,
    ) -> ScienceSuccessorRecoveryTargetOperationV1:
        try:
            return coordinator.get_target_operation(recovery_id, target_id)
        except ScienceSuccessorRecoveryV1Error as exc:
            status = 404 if "not found" in str(exc) else 409
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    @app.get(
        _PATH + "/{recovery_id}",
        response_model=ScienceSuccessorRecoverySnapshotV1,
        include_in_schema=False,
    )
    async def get_recovery(recovery_id: str) -> ScienceSuccessorRecoverySnapshotV1:
        try:
            return coordinator.get_recovery(recovery_id)
        except ScienceSuccessorRecoveryV1Error as exc:
            status = 404 if "not found" in str(exc) else 409
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    @app.post(
        _PATH + "/{recovery_id}/targets/{target_id}",
        response_model=ScienceSuccessorRecoverySnapshotV1,
        include_in_schema=False,
    )
    async def run_authorized_target(
        recovery_id: str,
        target_id: str,
        payload: ScienceSuccessorRecoveryTargetRunRequestV1,
    ) -> ScienceSuccessorRecoverySnapshotV1:
        try:
            return coordinator.run_authorized_target(
                recovery_id=recovery_id,
                target_id=target_id,
                idempotency_key=payload.idempotency_key,
            )
        except ScienceSuccessorRecoveryReadinessErrorV1 as exc:
            raise HTTPException(status_code=503, detail=exc.code) from exc
        except ScienceSuccessorRecoveryV1Error as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc


def install_core_managed_reflector_readiness_endpoint(
    app: FastAPI,
    provider: ManagedReflectorReadinessProviderV1,
) -> None:
    """Install a sanitized, bearer-protected current-worker readiness view."""

    if not hasattr(app.state, "core_control_v2_provider"):
        raise RuntimeError("managed reflector readiness requires authenticated Core v2")
    if getattr(app.state, "core_managed_reflector_readiness_installed", False):
        raise RuntimeError("managed reflector readiness endpoint is already installed")
    app.state.core_managed_reflector_readiness_installed = True

    @app.get(
        "/v2/internal/managed-reflector/readiness",
        response_model=ManagedReflectorReadinessResponseV1,
        include_in_schema=False,
    )
    async def get_managed_reflector_readiness(
    ) -> ManagedReflectorReadinessResponseV1:
        try:
            return provider.read_managed_reflector_readiness()
        except ScienceSuccessorRecoveryV1Error as exc:
            raise HTTPException(
                status_code=503,
                detail=getattr(exc, "code", "REFLECTOR_CREDENTIAL_MOUNT_NOT_READY"),
            ) from exc

__all__ = [
    "ManagedReflectorReadinessProviderV1",
    "ManagedReflectorReadinessResponseV1",
    "ScienceSuccessorRecoverySourceResolverV1",
    "ScienceSuccessorRecoveryProjectSeederV1",
    "ScienceSuccessorRecoveryTargetRunRequestV1",
    "install_core_managed_reflector_readiness_endpoint",
    "install_core_science_successor_recovery_endpoint",
]
