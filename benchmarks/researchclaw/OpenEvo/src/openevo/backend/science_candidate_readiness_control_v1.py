"""Bearer-protected no-model readiness for managed science candidates."""

from __future__ import annotations

from typing import Literal, Protocol

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from openevo.backend.service_supervisor import (
    ServiceExecutionMode,
    ServiceRunBinding,
    SupervisorStateError,
)
from openevo.runtime.managed import (
    MANAGED_CODEX_DEFAULT_MODEL,
    MANAGED_RUNTIME_IMAGES,
    managed_runtime_release_authority_digest,
)
from openevo.runtime.managed_candidate_readiness import (
    ManagedCandidateRuntimeReadiness,
    managed_candidate_readiness_failure_code,
)

_SHA256 = r"^[0-9a-f]{64}$"


class ManagedCandidateReadinessError(RuntimeError):
    """The current Core generation cannot prove candidate runtime readiness."""

    def __init__(self, message: str, *, code: str = "CANDIDATE_RUNTIME_NOT_READY") -> None:
        super().__init__(message)
        self.code = code


class ManagedCandidateReadinessResponseV1(BaseModel):
    """Current-generation readiness with release identity added by Core."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["openevo.managed_candidate_readiness.v1"] = (
        "openevo.managed_candidate_readiness.v1"
    )
    core_generation: str = Field(pattern=_SHA256)
    daemon_release_identity: str = Field(pattern=_SHA256)
    release_install_digest: str = Field(pattern=_SHA256)
    release_registry_digest: str = Field(pattern=_SHA256)
    framework_lock_digest: str = Field(pattern=_SHA256)
    runtime_identity_digest: str = Field(pattern=_SHA256)
    runtime_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    service_identity_id: Literal["core-gateway"]
    managed_candidate_runtime: ManagedCandidateRuntimeReadiness
    candidate_runtime_ready: Literal[True]
    credential_mount_adopted: Literal[True]
    codex_cli_started: Literal[True]
    model_started: Literal[False]
    secret_recorded: Literal[False]


class _ManagedServiceControl(Protocol):
    def adopt_current_run_binding(self, *args, **kwargs): ...

    def ensure_run_binding(self, *args, **kwargs): ...


class ProductionManagedCandidateReadinessProviderV1:
    """Read the Gateway canary through one leased, generation-bound binding."""

    def __init__(
        self,
        *,
        services: _ManagedServiceControl,
        daemon_release_identity: str,
        release_install_digest: str,
        codex_model: str = MANAGED_CODEX_DEFAULT_MODEL,
        timeout_seconds: float = 120.0,
    ) -> None:
        self._services = services
        self._daemon_release_identity = daemon_release_identity
        self._release_install_digest = release_install_digest
        self._codex_model = codex_model
        self._timeout_seconds = timeout_seconds

    def read_managed_candidate_readiness(
        self,
    ) -> ManagedCandidateReadinessResponseV1:
        try:
            try:
                current = self._services.adopt_current_run_binding(
                    ServiceExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT,
                    codex_model=self._codex_model,
                    runtime_image=MANAGED_RUNTIME_IMAGES["managed_science"],
                    total_timeout=self._timeout_seconds,
                )
            except SupervisorStateError:
                # A restarted Core marks its predecessor-owned service group
                # failed and clears every process identity.  That typed state
                # is not adoptable, but the normal ensure path can safely issue
                # a fresh generation after verifying the empty ownership set.
                current = None
            if current is None:
                snapshot, lease = self._services.ensure_run_binding(
                    ServiceExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT,
                    codex_model=self._codex_model,
                    runtime_image=MANAGED_RUNTIME_IMAGES["managed_science"],
                    total_timeout=self._timeout_seconds,
                )
            else:
                snapshot, lease = current
        except Exception as exc:
            raise ManagedCandidateReadinessError(
                "managed candidate readiness could not acquire a service binding"
            ) from exc
        binding = getattr(lease, "binding", None)
        if (
            lease is None
            or type(binding) is not ServiceRunBinding
            or getattr(snapshot, "run_ready", False) is not True
        ):
            if lease is not None:
                close = getattr(lease, "close", None)
                if callable(close):
                    close()
            try:
                service_error_code = snapshot.service("gateway").error_code
            except (AttributeError, KeyError):
                service_error_code = None
            raise ManagedCandidateReadinessError(
                "managed candidate readiness lacks a verified service binding",
                code=managed_candidate_readiness_failure_code(service_error_code),
            )
        try:
            with httpx.Client(
                base_url=binding.gateway_url,
                headers=binding.request_headers(),
                timeout=min(self._timeout_seconds, 30.0),
                trust_env=False,
            ) as client:
                response = client.get("/health")
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            raise ManagedCandidateReadinessError(
                "managed candidate readiness could not read Gateway authority"
            ) from exc
        finally:
            lease.close()
        try:
            readiness = ManagedCandidateRuntimeReadiness.model_validate(
                payload.get("managed_candidate_runtime_readiness")
            )
        except (TypeError, ValueError) as exc:
            raise ManagedCandidateReadinessError(
                "managed candidate Gateway receipt is invalid"
            ) from exc
        runtime_digest = managed_runtime_release_authority_digest(
            profile="managed_science",
            image=binding.runtime_image_immutable_reference,
        )
        if (
            readiness.generation_digest != binding.generation_digest
            or readiness.release_registry_digest != binding.registry_digest
            or readiness.framework_lock_digest != binding.framework_lock_digest
            or readiness.runtime_digest != runtime_digest
            or readiness.docker_host_path_identity == ""
            or readiness.model_started is not False
        ):
            raise ManagedCandidateReadinessError(
                "managed candidate Gateway receipt identity changed"
            )
        return ManagedCandidateReadinessResponseV1(
            core_generation=binding.generation_digest,
            daemon_release_identity=self._daemon_release_identity,
            release_install_digest=self._release_install_digest,
            release_registry_digest=binding.registry_digest,
            framework_lock_digest=binding.framework_lock_digest,
            runtime_identity_digest=binding.runtime_identity_digest,
            runtime_digest=runtime_digest,
            service_identity_id="core-gateway",
            managed_candidate_runtime=readiness,
            candidate_runtime_ready=True,
            credential_mount_adopted=True,
            codex_cli_started=True,
            model_started=False,
            secret_recorded=False,
        )


def install_core_managed_candidate_readiness_endpoint(
    app: FastAPI,
    provider: ProductionManagedCandidateReadinessProviderV1,
) -> None:
    """Install one private, Core-bearer authenticated readiness route."""

    if not hasattr(app.state, "core_control_v2_provider"):
        raise RuntimeError("managed candidate readiness requires authenticated Core v2")
    if getattr(app.state, "core_managed_candidate_readiness_installed", False):
        raise RuntimeError("managed candidate readiness endpoint is already installed")
    app.state.core_managed_candidate_readiness_installed = True

    @app.get(
        "/v2/internal/managed-candidate/readiness",
        response_model=ManagedCandidateReadinessResponseV1,
        include_in_schema=False,
    )
    async def get_managed_candidate_readiness() -> ManagedCandidateReadinessResponseV1:
        try:
            return provider.read_managed_candidate_readiness()
        except ManagedCandidateReadinessError as exc:
            raise HTTPException(
                status_code=503,
                detail=exc.code,
            ) from exc


__all__ = [
    "ManagedCandidateReadinessError",
    "ManagedCandidateReadinessResponseV1",
    "ProductionManagedCandidateReadinessProviderV1",
    "install_core_managed_candidate_readiness_endpoint",
]
