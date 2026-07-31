"""Authenticated, immutable Core authority for a frozen project head.

The freeze is deliberately separate from benchmark policy.  Core verifies one
active project head and its exact promoted artifact composition, then seals a
single append-only authority.  Callers may attach protocol and runtime digests
as opaque identities, but cannot use them to change the Core-owned head.
"""

from __future__ import annotations

from typing import Literal, Protocol

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from openevo.backend.contracts.v2 import models as m2
from openevo.evolution.revisions import (
    AtomicFrozenProjectForkManifestV1,
    AtomicSuccessorCommitV2,
)


class FrozenProjectArtifactV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_type: Literal["agent_system", "text_memory", "skill_bundle"]
    registry_id: str = Field(min_length=1, max_length=256)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    frozen: Literal[True] = True


class ProjectFreezeRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openevo.project_freeze_request.v1"] = (
        "openevo.project_freeze_request.v1"
    )
    project_id: str = Field(min_length=1, max_length=256)
    expected_project_head_id: str = Field(min_length=1, max_length=256)
    expected_project_head_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    composite_id: str = Field(min_length=1, max_length=256)
    composite_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifacts: tuple[FrozenProjectArtifactV1, ...]
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    core_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    adapter_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_digest: str = Field(pattern=r"^(?:sha256:)?[0-9a-f]{64}$")
    codex_cli_version: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=256)
    reasoning_effort: str = Field(min_length=1, max_length=64)
    training_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    budget_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def _exact_triple(self) -> ProjectFreezeRequestV1:
        expected = ("agent_system", "skill_bundle", "text_memory")
        observed = tuple(sorted(item.artifact_type for item in self.artifacts))
        if observed != expected or len({item.registry_id for item in self.artifacts}) != 3:
            raise ValueError("project freeze requires one authority for each text artifact")
        return self


class ProjectFreezeAuthorityV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openevo.project_freeze_authority.v1"] = (
        "openevo.project_freeze_authority.v1"
    )
    freeze_receipt_id: str
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authority_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    project_id: str
    project_head_id: str
    project_head_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    composite_id: str
    composite_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifacts: tuple[FrozenProjectArtifactV1, ...]
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    core_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    adapter_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_digest: str
    codex_cli_version: str
    model: str
    reasoning_effort: str
    training_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    budget_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: str
    core_authoritative: Literal[True] = True
    frozen: Literal[True] = True
    evolution_enabled: Literal[False] = False


class FrozenProjectForkRequestV1(BaseModel):
    """Create one append-only destination seed from a frozen source head."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openevo.frozen_project_fork_request.v1"] = (
        "openevo.frozen_project_fork_request.v1"
    )
    source_project_id: str = Field(min_length=1, max_length=256)
    source_freeze_receipt_id: str = Field(min_length=1, max_length=256)
    source_freeze_authority_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_source_project_head_id: str = Field(min_length=1, max_length=256)
    expected_source_project_head_manifest_sha256: str = Field(
        pattern=r"^[0-9a-f]{64}$"
    )
    destination_project_id: str = Field(min_length=1, max_length=256)
    expected_destination_project_head_id: str = Field(
        min_length=1,
        max_length=256,
    )
    expected_destination_project_head_manifest_sha256: str = Field(
        pattern=r"^[0-9a-f]{64}$"
    )
    expected_destination_project_config_sha256: str = Field(
        pattern=r"^[0-9a-f]{64}$"
    )
    idempotency_key: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def _independent_projects(self) -> FrozenProjectForkRequestV1:
        if self.source_project_id == self.destination_project_id:
            raise ValueError("frozen project fork requires an independent destination")
        return self


class FrozenProjectForkAuthorityV1(BaseModel):
    """Immutable Core receipt for one frozen project fork seed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openevo.frozen_project_fork_authority.v1"] = (
        "openevo.frozen_project_fork_authority.v1"
    )
    fork_receipt_id: str
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authority_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_project_id: str
    source_freeze_receipt_id: str
    source_freeze_authority_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_project_head_id: str
    source_project_head_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    destination_project_id: str
    destination_project_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    predecessor_destination_project_head_id: str
    predecessor_destination_project_head_manifest_sha256: str = Field(
        pattern=r"^[0-9a-f]{64}$"
    )
    seeded_destination_project_head: m2.ProjectHeadRefV2
    artifacts: tuple[FrozenProjectArtifactV1, ...]
    commit: AtomicSuccessorCommitV2
    created_at: str
    core_authoritative: Literal[True] = True
    append_only: Literal[True] = True
    source_frozen: Literal[True] = True
    source_mutated: Literal[False] = False
    destination_seeded: Literal[True] = True
    evolution_enabled: Literal[False] = False

    @model_validator(mode="after")
    def _closed_fork(self) -> FrozenProjectForkAuthorityV1:
        head = self.seeded_destination_project_head
        if (
            type(self.commit.manifest) is not AtomicFrozenProjectForkManifestV1
            or self.source_project_id == self.destination_project_id
            or head.project_id != self.destination_project_id
            or head.predecessor_project_head_id
            != self.predecessor_destination_project_head_id
            or tuple(item.artifact_type for item in self.artifacts)
            != ("agent_system", "skill_bundle", "text_memory")
            or tuple(item.registry_id for item in self.artifacts)
            != self.commit.manifest.method_artifact_ids
        ):
            raise ValueError("frozen project fork authority closure is invalid")
        return self


class ProjectFreezeOwner(Protocol):
    def freeze_project(
        self,
        request: ProjectFreezeRequestV1,
        *,
        verified_artifacts: tuple[FrozenProjectArtifactV1, ...],
    ) -> ProjectFreezeAuthorityV1: ...

    def project_freeze(self, project_id: str) -> ProjectFreezeAuthorityV1: ...

    def fork_frozen_project(
        self,
        request: FrozenProjectForkRequestV1,
    ) -> FrozenProjectForkAuthorityV1: ...

    def frozen_project_fork(
        self,
        fork_request_id: str,
    ) -> FrozenProjectForkAuthorityV1: ...


class ProjectFreezeArtifactReader(Protocol):
    def verified_artifacts_for_project_head(
        self, project_id: str, project_head_id: str
    ) -> tuple[FrozenProjectArtifactV1, ...]: ...


def install_core_project_freeze_endpoint(
    app: FastAPI,
    owner: ProjectFreezeOwner,
    artifact_reader: ProjectFreezeArtifactReader,
) -> None:
    if not hasattr(app.state, "core_control_v2_provider"):
        raise RuntimeError("project freeze requires authenticated Core v2")
    if getattr(app.state, "core_project_freeze_installed", False):
        raise RuntimeError("project freeze endpoint is already installed")
    app.state.core_project_freeze_installed = True

    @app.post(
        "/v2/internal/project-freezes",
        response_model=ProjectFreezeAuthorityV1,
        include_in_schema=False,
    )
    async def create_project_freeze(
        request: ProjectFreezeRequestV1,
    ) -> ProjectFreezeAuthorityV1:
        try:
            verified = artifact_reader.verified_artifacts_for_project_head(
                request.project_id,
                request.expected_project_head_id,
            )
            return owner.freeze_project(request, verified_artifacts=verified)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get(
        "/v2/internal/project-freezes/{project_id}",
        response_model=ProjectFreezeAuthorityV1,
        include_in_schema=False,
    )
    async def get_project_freeze(project_id: str) -> ProjectFreezeAuthorityV1:
        try:
            return owner.project_freeze(project_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post(
        "/v2/internal/frozen-project-forks",
        response_model=FrozenProjectForkAuthorityV1,
        include_in_schema=False,
    )
    async def create_frozen_project_fork(
        request: FrozenProjectForkRequestV1,
    ) -> FrozenProjectForkAuthorityV1:
        try:
            return owner.fork_frozen_project(request)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get(
        "/v2/internal/frozen-project-forks/{fork_request_id}",
        response_model=FrozenProjectForkAuthorityV1,
        include_in_schema=False,
    )
    async def get_frozen_project_fork(
        fork_request_id: str,
    ) -> FrozenProjectForkAuthorityV1:
        try:
            return owner.frozen_project_fork(fork_request_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


__all__ = [
    "FrozenProjectArtifactV1",
    "FrozenProjectForkAuthorityV1",
    "FrozenProjectForkRequestV1",
    "ProjectFreezeArtifactReader",
    "ProjectFreezeAuthorityV1",
    "ProjectFreezeOwner",
    "ProjectFreezeRequestV1",
    "install_core_project_freeze_endpoint",
]
