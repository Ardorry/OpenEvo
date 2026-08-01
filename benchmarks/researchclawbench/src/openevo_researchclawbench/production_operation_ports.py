"""Production authority adapters used by :mod:`production_training_operations`.

Network calls target authenticated OpenEvo Core v2.  Candidate execution and
successor evolution remain Core-owned; the adapter only uploads a sanitized
workspace, submits/polls immutable authority, and exports the sealed result.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import secrets
import shutil
import tarfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

import httpx

from openevo.backend.contracts.v2.models import (
    CapabilitiesResponseV2,
    ProjectCreateV2,
    ProjectHeadRefV2,
    ScienceProjectConfigV2,
    WorkspaceArchiveDeclarationV2,
    WorkspaceSnapshotRefV2,
    WorkspaceUploadCreateV2,
)
from openevo.backend.training_attempt_control import (
    TrainingAttemptExecutionStatusV1,
)
from openevo.evolution.framework.schema import normalize_config_override
from openevo.evolution.models import ArtifactContentAdmissionReceipt
from openevo.evolution.training_feedback import (
    training_feedback_attachment_id_for_idempotency_key,
)
from openevo.rollout.models import SessionResult
from openevo.runtime.codex_isolation import (
    CODEX_SUBSCRIPTION_POLICY_ID,
    CODEX_SUBSCRIPTION_POLICY_SHA256,
)
from openevo.workspace_archive import write_workspace_archive

from .artifact_validator import freeze_candidate_outputs, validate_workspace
from .community_evaluator import (
    DurableCommunityEvaluatorPort,
    build_production_community_evaluator,
)
from .config import ARTIFACT_TYPES, ExperimentConfig
from .managed_core_control import ManagedCoreControlAuthority
from .production_training_operations import (
    ProductionOperationPort,
    ProductionPorts,
    SuccessorRecoveryRequired,
)
from .prompt_composer import compose_native_instruction
from .reflector_runner import NATIVE_METHODS
from .run_manifest import atomic_write_json
from .training_state_store import canonical_bytes, canonical_sha256
from .training_supervisor import CandidateAuthorityUnavailable
from .workspace import (
    CANDIDATE_VISIBLE_ENTRIES,
    WorkspaceReceipt,
    assert_candidate_workspace_shape,
    build_official_workspace,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VALIDATOR_FAILURE_TAGS = frozenset(
    {
        "UNSAFE_FILESYSTEM_ENTRY",
        "CODE_DIRECTORY_MISSING",
        "OUTPUTS_DIRECTORY_MISSING",
        "REPORT_DIRECTORY_MISSING",
        "REPORT_MISSING",
        "REPORT_INSUFFICIENT_SUBSTANCE",
        "REPORT_REQUIRED_SECTIONS_MISSING",
        "UNFINISHED_MARKER",
        "ABSOLUTE_PATH_REFERENCE",
        "PNG_MISSING",
        "PNG_INVALID",
        "IMAGE_REFERENCE_INVALID",
        "CODE_MISSING",
        "OUTPUTS_MISSING",
        "TRACEABILITY_MISSING",
        "EXIT_STATUS_INCONSISTENT",
        "TIMEOUT",
    }
)

_CORE_CONTROL_CLOSED_ERROR_CODE = re.compile(
    r"^(?:[A-Z][A-Z0-9_]{1,95}(?::[a-z][a-z0-9_]{1,95})?"
    r"|[a-z][a-z0-9_]{1,95})$"
)


def _closed_core_control_error_code(body: object, *, path: str = "") -> str:
    """Project only one stable, non-secret Core error code from an HTTP body."""

    if not isinstance(body, dict):
        return "closed"
    recovery_codes = {
        "failed recovery Evolution inventory identity changed": (
            "RECOVERY_EVOLUTION_INVENTORY_IDENTITY_CHANGED"
        ),
        "failed recovery owns a non-adoptable paid Evolution side effect": (
            "RECOVERY_PAID_SIDE_EFFECT_NOT_ADOPTABLE"
        ),
        "failed recovery inventory omitted a completed target job": (
            "RECOVERY_COMPLETED_JOB_OMITTED"
        ),
        "recovery supersession carried target authority changed": (
            "RECOVERY_CARRIED_TARGET_AUTHORITY_CHANGED"
        ),
        "recovery supersession completed target inventory is incomplete": (
            "RECOVERY_COMPLETED_TARGET_INVENTORY_INCOMPLETE"
        ),
        "recovery supersession completed target evidence is invalid": (
            "RECOVERY_COMPLETED_TARGET_EVIDENCE_INVALID"
        ),
        "recovery supersession omitted completed target authority": (
            "RECOVERY_COMPLETED_TARGET_AUTHORITY_OMITTED"
        ),
        "carried recovery target differs from prior sealed evidence": (
            "RECOVERY_CARRIED_TARGET_SEALED_EVIDENCE_CHANGED"
        ),
        "recovery supersession authority differs from terminal evidence": (
            "RECOVERY_SUPERSESSION_TERMINAL_EVIDENCE_CHANGED"
        ),
    }
    detail = body.get("detail")
    if isinstance(detail, str) and detail in recovery_codes:
        return recovery_codes[detail]
    if isinstance(detail, str) and detail.startswith(
        (
            "recovery ",
            "failed recovery ",
            "carried recovery ",
            "only one terminal failed recovery ",
        )
    ) and re.fullmatch(r"[A-Za-z ]{3,160}", detail):
        normalized = re.sub(r"[^A-Za-z0-9]+", "_", detail).strip("_").upper()
        if 2 <= len(normalized) <= 95:
            return normalized
    if (
        path.startswith("/v2/internal/science-successor-recoveries")
        and isinstance(detail, str)
        and re.fullmatch(r"[A-Za-z ]{3,160}", detail)
    ):
        normalized = re.sub(r"[^A-Za-z0-9]+", "_", detail).strip("_").upper()
        if 2 <= len(normalized) <= 95:
            return normalized
    for field in ("code", "detail"):
        value = body.get(field)
        if isinstance(value, str) and _CORE_CONTROL_CLOSED_ERROR_CODE.fullmatch(value):
            return value
    return "closed"


def _workspace_snapshot_for_archive(
    *,
    project_id: str,
    archive: WorkspaceArchiveDeclarationV2,
) -> WorkspaceSnapshotRefV2:
    """Derive the public v2 snapshot identity from its archive declaration."""

    manifest_sha256 = canonical_sha256(
        {
            "archive": archive.model_dump(mode="json"),
            "manifest_contract_version": "2",
            "project_id": project_id,
        }
    )
    return WorkspaceSnapshotRefV2(
        workspace_snapshot_id=f"workspace-{manifest_sha256}",
        project_id=project_id,
        manifest_sha256=manifest_sha256,
        entry_count=archive.entry_count,
        byte_size=archive.extracted_byte_size,
    )


def _successor_workspace_projection_authority(
    *,
    core_archive: WorkspaceArchiveDeclarationV2,
    sanitized_archive: WorkspaceArchiveDeclarationV2,
    stripped_entries: list[dict[str, Any]],
) -> dict[str, Any]:
    """Bind the Core snapshot to its candidate-visible sanitized projection.

    Core deliberately captures the injected root ``AGENTS.md`` in a completed
    workspace result.  The adapter deliberately removes that transient runtime
    target (and exact empty runtime scaffolding directories) before it stores a
    successor handoff.  Both archive identities are authoritative: the Core
    project head owns the former, while the next local handoff owns the latter.
    """

    normalized = json.loads(canonical_bytes(stripped_entries))
    if not isinstance(normalized, list) or any(
        not isinstance(item, dict) for item in normalized
    ):
        raise ValueError("successor workspace projection entry is invalid")
    normalized.sort(key=lambda item: str(item.get("relative_path")))
    seen: set[str] = set()
    stripped_bytes = 0
    for item in normalized:
        if not isinstance(item, dict):
            raise ValueError("successor workspace projection entry is invalid")
        path = item.get("relative_path")
        kind = item.get("kind")
        if not isinstance(path, str) or path in seen:
            raise ValueError("successor workspace projection entry is invalid")
        seen.add(path)
        if kind == "empty_runtime_scaffold":
            if set(item) != {"kind", "relative_path", "size_bytes"} or (
                path not in {".agents", ".codex", ".git"}
                or item.get("size_bytes") != 0
            ):
                raise ValueError("successor workspace projection entry is invalid")
            continue
        if kind == "runtime_agent_system":
            if (
                set(item)
                != {
                    "kind",
                    "relative_path",
                    "runtime_receipt_path",
                    "sha256",
                    "size_bytes",
                }
                or path != "AGENTS.md"
                or item.get("runtime_receipt_path")
                != "agent_system_targets/AGENTS.md"
                or isinstance(item.get("size_bytes"), bool)
                or not isinstance(item.get("size_bytes"), int)
                or item["size_bytes"] < 0
                or not isinstance(item.get("sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is None
            ):
                raise ValueError("successor workspace projection entry is invalid")
            stripped_bytes += item["size_bytes"]
            continue
        raise ValueError("successor workspace projection entry is invalid")
    if (
        core_archive.entry_count - sanitized_archive.entry_count != len(normalized)
        or core_archive.extracted_byte_size
        - sanitized_archive.extracted_byte_size
        != stripped_bytes
        or core_archive.entry_count < sanitized_archive.entry_count
        or core_archive.extracted_byte_size < sanitized_archive.extracted_byte_size
    ):
        raise ValueError("successor workspace projection archive delta is invalid")
    if not normalized and core_archive != sanitized_archive:
        raise ValueError("successor workspace projection changed an unstripped archive")
    body = {
        "schema_version": (
            "openevo.researchclawbench.successor_workspace_projection.v1"
        ),
        "core_output_archive": core_archive.model_dump(mode="json"),
        "sanitized_output_archive": sanitized_archive.model_dump(mode="json"),
        "stripped_entry_count": len(normalized),
        "stripped_byte_size": stripped_bytes,
        "stripped_entries": normalized,
    }
    return {**body, "content_sha256": canonical_sha256(body)}


def _require_successor_workspace_projection(
    *,
    authority: object,
    core_archive: WorkspaceArchiveDeclarationV2,
    sanitized_archive: WorkspaceArchiveDeclarationV2,
) -> dict[str, Any]:
    if not isinstance(authority, dict) or set(authority) != {
        "schema_version",
        "core_output_archive",
        "sanitized_output_archive",
        "stripped_entry_count",
        "stripped_byte_size",
        "stripped_entries",
        "content_sha256",
    }:
        raise CoreControlError(
            "CANDIDATE_SUCCESSOR_WORKSPACE_AUTHORITY_INVALID",
            reason_code="candidate_successor_workspace_authority_invalid",
        )
    try:
        expected = _successor_workspace_projection_authority(
            core_archive=core_archive,
            sanitized_archive=sanitized_archive,
            stripped_entries=authority["stripped_entries"],
        )
    except (TypeError, ValueError) as exc:
        raise CoreControlError(
            "CANDIDATE_SUCCESSOR_WORKSPACE_AUTHORITY_INVALID",
            reason_code="candidate_successor_workspace_authority_invalid",
        ) from exc
    if authority != expected:
        raise CoreControlError(
            "CANDIDATE_SUCCESSOR_WORKSPACE_AUTHORITY_DRIFT",
            reason_code="candidate_successor_workspace_authority_drift",
        )
    return expected


def _require_preseeded_workspace_authority(
    *,
    authority: object,
    project: dict[str, Any],
    project_head: dict[str, Any],
    archive: WorkspaceArchiveDeclarationV2,
) -> dict[str, Any]:
    """Verify that a published recovery head already owns this fresh workspace.

    A published project cannot accept a direct workspace upload.  Recovery
    continuation therefore seeds the fresh Attempt workspace before it commits
    the recovered artifact head.  This closed receipt proves that the archive
    about to be consumed is exactly that pre-seeded workspace; it never turns a
    mismatched published head into a writable draft.
    """

    required = {
        "schema_version",
        "project_id",
        "project_head_id",
        "project_head_manifest_sha256",
        "workspace_snapshot_id",
        "workspace_manifest_sha256",
        "archive_content_sha256",
        "archive_byte_size",
        "archive_entry_count",
        "archive_extracted_byte_size",
        "seed_request_id",
        "seed_sha256",
    }
    if not isinstance(authority, dict) or set(authority) != required:
        raise CoreControlError(
            "CANDIDATE_PRESEEDED_WORKSPACE_AUTHORITY_INVALID",
            reason_code="candidate_preseeded_workspace_authority_invalid",
        )
    expected_snapshot = _workspace_snapshot_for_archive(
        project_id=str(project["project_id"]),
        archive=archive,
    ).model_dump(mode="json")
    observed_snapshot = project_head.get("workspace_snapshot")
    expected = {
        "schema_version": "openevo.researchclawbench.preseeded_workspace_authority.v1",
        "project_id": project["project_id"],
        "project_head_id": project_head["project_head_id"],
        "project_head_manifest_sha256": project_head["manifest_sha256"],
        "workspace_snapshot_id": expected_snapshot["workspace_snapshot_id"],
        "workspace_manifest_sha256": expected_snapshot["manifest_sha256"],
        "archive_content_sha256": archive.content_sha256,
        "archive_byte_size": archive.byte_size,
        "archive_entry_count": archive.entry_count,
        "archive_extracted_byte_size": archive.extracted_byte_size,
        "seed_request_id": authority.get("seed_request_id"),
        "seed_sha256": authority.get("seed_sha256"),
    }
    if (
        authority != expected
        or observed_snapshot != expected_snapshot
        or project.get("state") != "ready"
        or not isinstance(authority.get("seed_request_id"), str)
        or not authority["seed_request_id"]
        or not isinstance(authority.get("seed_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", authority["seed_sha256"]) is None
    ):
        raise CoreControlError(
            "CANDIDATE_PRESEEDED_WORKSPACE_AUTHORITY_DRIFT",
            reason_code="candidate_preseeded_workspace_authority_drift",
        )
    return expected_snapshot


def _require_existing_workspace_snapshot(
    *,
    project: dict[str, Any],
    project_head: dict[str, Any],
    archive: WorkspaceArchiveDeclarationV2,
    allow_recovery_needs_attention: bool = False,
) -> dict[str, Any]:
    """Admit a normal later attempt against the immutable task workspace.

    A successor evolution head preserves its predecessor workspace snapshot.
    Later attempts for the same task therefore do not upload or seed another
    workspace: they prove that the newly materialized sanitized archive is
    exactly the snapshot already owned by the selected Core head.  Recovery
    continuations remain on the stronger pre-seeded authority path above.
    """

    expected_snapshot = _workspace_snapshot_for_archive(
        project_id=str(project["project_id"]),
        archive=archive,
    ).model_dump(mode="json")
    project_state = project.get("state")
    state_matches = project_state == "ready" or (
        allow_recovery_needs_attention and project_state == "needs_attention"
    )
    if not state_matches or project_head.get("workspace_snapshot") != expected_snapshot:
        raise CoreControlError(
            "CANDIDATE_EXISTING_WORKSPACE_SNAPSHOT_DRIFT",
            reason_code="candidate_existing_workspace_snapshot_drift",
        )
    return expected_snapshot


def _candidate_workspace_binding_receipt(
    *,
    task_id: str,
    attempt_index: int,
    workspace_source: str,
    workspace_root: Path,
    project: dict[str, Any],
    project_head: dict[str, Any],
    archive: WorkspaceArchiveDeclarationV2,
    core_archive: WorkspaceArchiveDeclarationV2 | None,
    workspace_projection: dict[str, Any] | None,
    expected_snapshot: dict[str, Any],
    task_local_overlay_id: object,
) -> dict[str, Any]:
    """Close the Core-owned and candidate-visible workspace proof."""

    core_workspace_archive = core_archive or archive
    sanitized_snapshot = _workspace_snapshot_for_archive(
        project_id=str(project["project_id"]),
        archive=archive,
    ).model_dump(mode="json")
    if workspace_projection is None:
        if core_workspace_archive != archive:
            raise CoreControlError(
                "CANDIDATE_SUCCESSOR_WORKSPACE_AUTHORITY_INVALID",
                reason_code="candidate_successor_workspace_authority_invalid",
            )
    else:
        workspace_projection = _require_successor_workspace_projection(
            authority=workspace_projection,
            core_archive=core_workspace_archive,
            sanitized_archive=archive,
        )

    body = {
        "schema_version": (
            "openevo.researchclawbench.candidate_workspace_binding.v2"
        ),
        "task_id": task_id,
        "attempt_index": attempt_index,
        "workspace_source": workspace_source,
        "workspace_root": os.fspath(workspace_root.resolve(strict=True)),
        "project_id": project["project_id"],
        "project_head_id": project_head["project_head_id"],
        "project_head_manifest_sha256": project_head["manifest_sha256"],
        "archive": archive.model_dump(mode="json"),
        "core_workspace_archive": core_workspace_archive.model_dump(mode="json"),
        "sanitized_workspace_snapshot": sanitized_snapshot,
        "workspace_projection": workspace_projection,
        "expected_workspace_snapshot": expected_snapshot,
        "actual_workspace_snapshot": project_head.get("workspace_snapshot"),
        "workspace_snapshot_match": (
            project_head.get("workspace_snapshot") == expected_snapshot
        ),
        "task_local_overlay_id": task_local_overlay_id,
        "model_started": False,
        "core_task_created": False,
    }
    return {**body, "content_sha256": canonical_sha256(body)}


def build_validator_feedback_layers(
    candidate: dict[str, Any], validation: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build closed global/task-local layers from a public validator receipt."""

    if validation.get("artifact_valid") is not False:
        raise ValueError("validator feedback requires an invalid artifact")
    tags = validation.get("validator_errors", [])
    if (
        not isinstance(tags, list)
        or not tags
        or any(item not in _VALIDATOR_FAILURE_TAGS for item in tags)
    ):
        raise ValueError("validator feedback contains a non-allowlisted failure tag")
    root = Path(candidate["candidate_output_root"]).resolve(strict=True)
    report = root / "report" / "report.md"
    if not report.is_relative_to(root) or report.is_symlink():
        raise ValueError("validator feedback report path is unsafe")
    headings: list[str] = []
    if report.is_file():
        for line in report.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                heading = stripped.lstrip("#").strip()
                if heading:
                    headings.append(heading[:160])
            if len(headings) == 64:
                break
    runtime = max(0, int(float(candidate.get("runtime_seconds", 0.0))))
    global_feedback = {
        "completed": candidate.get("completed") is True,
        "exit_code": 0 if candidate.get("completed") is True else 1,
        "artifact_valid": False,
        "generic_failure_tags": sorted({"ARTIFACT_STRUCTURE_INCOMPLETE", *tags}),
        "runtime_bucket_seconds": ((runtime + 59) // 60) * 60,
        "cost_total_usd": candidate.get("cost_total_usd"),
        "artifact_root_sha256": validation.get("artifact_root_sha256"),
    }
    task_local_feedback = {
        "feedback_source": "artifact_validator",
        "validator_failure_tags": tags,
        "report_headings": headings,
        "required_sections_missing": (
            ["methods", "results", "discussion_or_limitations"]
            if "REPORT_REQUIRED_SECTIONS_MISSING" in tags
            else []
        ),
        "completion_requirements": [
            "run the report structure preflight before declaring completion",
            "verify required sections contain substantive content",
            "distinguish file existence from artifact completeness",
        ],
    }
    return global_feedback, task_local_feedback


class CoreControlError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        reason_code: str = "core_control_error",
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.status_code = status_code


class CoreControlV2Client:
    """Generation-bound closed client over an in-memory Core attachment."""

    def __init__(self, authority: ManagedCoreControlAuthority) -> None:
        if not isinstance(authority, ManagedCoreControlAuthority):
            raise CoreControlError("MANAGED_CORE_CONTROL_AUTHORITY_REQUIRED")
        self.base_url = authority.base_url.rstrip("/")
        self._generation = authority.generation
        self._release_identity = authority.release_identity
        self._client = httpx.Client(
            timeout=httpx.Timeout(60.0, connect=10.0),
            trust_env=False,
            headers={
                "Authorization": (
                    f"Bearer {authority.bearer_value_for_core_client()}"
                )
            },
        )

    def close(self) -> None:
        self._client.close()

    def json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        request_kwargs: dict[str, Any] = {
            "json": payload,
            "content": content,
            "headers": headers,
        }
        if timeout_seconds is not None:
            if (
                isinstance(timeout_seconds, bool)
                or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(float(timeout_seconds))
                or not 0.0 < float(timeout_seconds) <= 86_400.0
            ):
                raise CoreControlError(
                    "CORE_CONTROL_TIMEOUT_INVALID",
                    reason_code="core_control_timeout_invalid",
                )
            request_kwargs["timeout"] = httpx.Timeout(
                float(timeout_seconds),
                connect=10.0,
            )
        try:
            response = self._client.request(
                method,
                self.base_url + path,
                **request_kwargs,
            )
        except httpx.RemoteProtocolError:
            # A long-running Candidate poll can encounter one stale HTTP/1.1
            # connection after the SSH-forwarded peer closes keep-alive.  A
            # single retry is safe only for GET: Core mutations continue to
            # surface so their idempotency authority is reconciled explicitly.
            if method.upper() != "GET":
                raise
            response = self._client.request(
                method,
                self.base_url + path,
                **request_kwargs,
            )
        if not 200 <= response.status_code < 300:
            body: object = None
            try:
                body = response.json()
            except ValueError:
                pass
            safe_code = _closed_core_control_error_code(body, path=path)
            raise CoreControlError(
                f"Core v2 {method} {path} failed: {response.status_code}:{safe_code}",
                reason_code=safe_code,
                status_code=response.status_code,
            )
        self._require_generation(response)
        result = response.json()
        if not isinstance(result, dict):
            raise CoreControlError("Core v2 response is not an object")
        return result

    def bytes(self, path: str) -> tuple[bytes, dict[str, str]]:
        response = self._client.get(self.base_url + path)
        if response.status_code != 200:
            raise CoreControlError(f"Core v2 workspace result failed: {response.status_code}")
        self._require_generation(response)
        return response.content, dict(response.headers)

    def _require_generation(self, response: httpx.Response) -> None:
        if (
            response.headers.get("X-OpenEvo-Core-Generation") != self._generation
            or response.headers.get("X-OpenEvo-Core-Release-Identity")
            != self._release_identity
        ):
            raise CoreControlError("CORE_CONTROL_GENERATION_MISMATCH")

    def readiness(self) -> dict[str, Any]:
        version = self.json("GET", "/version")
        status = self.json("GET", "/v2/system/status")
        if (
            version.get("preferred_major") != 2
            or version.get("provider_kind") != "openevo_daemon"
            or status.get("status") != "ready"
        ):
            raise CoreControlError("CORE_CONTROL_V2_NOT_READY")
        return {
            "service_reachable": True,
            "bearer_present": True,
            "bearer_valid": True,
            "generation_matches": True,
            "candidate_port_authorized": True,
            "secret_recorded": False,
            "generation": self._generation,
            "release_identity": self._release_identity,
        }


def require_managed_candidate_isolation_readiness(
    *,
    candidate_runtime: dict[str, Any],
    core_readiness: dict[str, Any],
    expected_registry_digest: str,
    failure_code: str = "CANDIDATE_RUNTIME_NOT_READY",
) -> dict[str, Any]:
    """Validate the one production Candidate isolation authority.

    Community and frozen-official callers deliberately share this exact
    contract so a second, weaker readiness interpretation cannot drift in.
    """

    runtime_receipt = candidate_runtime.get("managed_candidate_runtime")
    if (
        candidate_runtime.get("core_generation")
        != (
            runtime_receipt.get("generation_digest")
            if isinstance(runtime_receipt, dict)
            else None
        )
        or candidate_runtime.get("daemon_release_identity")
        != core_readiness.get("release_identity")
        or candidate_runtime.get("release_registry_digest")
        != expected_registry_digest
        or candidate_runtime.get("candidate_runtime_ready") is not True
        or candidate_runtime.get("credential_mount_adopted") is not True
        or candidate_runtime.get("codex_cli_started") is not True
        or candidate_runtime.get("model_started") is not False
        or candidate_runtime.get("secret_recorded") is not False
        or not isinstance(runtime_receipt, dict)
        or runtime_receipt.get("actual_cli_version") != "0.144.1"
        or runtime_receipt.get("adoption_verified") is not True
        or runtime_receipt.get("cleanup_verified") is not True
        or runtime_receipt.get("path_fallback_allowed") is not False
        or runtime_receipt.get("model_started") is not False
        or runtime_receipt.get("codex_cli_auth_visible") is not True
        or runtime_receipt.get("host_source_hidden") is not True
        or runtime_receipt.get("tool_sandbox_credential_hidden") is not True
        or runtime_receipt.get("tool_environment_clean") is not True
        or runtime_receipt.get("parent_process_secret_unreadable") is not True
        or runtime_receipt.get("generation_matches") is not True
        or runtime_receipt.get("image_digest_matches") is not True
        or runtime_receipt.get("isolation_probe_no_model") is not True
        or runtime_receipt.get("isolation_policy_id")
        != CODEX_SUBSCRIPTION_POLICY_ID
        or runtime_receipt.get("isolation_policy_sha256")
        != CODEX_SUBSCRIPTION_POLICY_SHA256
    ):
        raise CoreControlError(failure_code)
    return runtime_receipt


def require_nonterminal_candidate_lifecycle(
    payload: dict[str, Any],
    *,
    task_id: str,
    attempt_id: str,
) -> bool:
    """Return whether sealed authority is readable or raise a typed failure."""

    try:
        lifecycle = TrainingAttemptExecutionStatusV1.model_validate(payload)
    except (TypeError, ValueError) as exc:
        raise CoreControlError(
            "CANDIDATE_EXECUTION_STATUS_INVALID",
            reason_code="candidate_execution_status_invalid",
        ) from exc
    if lifecycle.task_id != task_id or lifecycle.attempt_id != attempt_id:
        raise CoreControlError(
            "CANDIDATE_EXECUTION_IDENTITY_DRIFT",
            reason_code="candidate_execution_identity_drift",
        )
    if lifecycle.state in {"failed", "cancelled"}:
        failure = lifecycle.failure_authority
        if lifecycle.state == "failed" and failure is None:
            raise CoreControlError(
                "CANDIDATE_TERMINAL_FAILURE_AUTHORITY_INVALID",
                reason_code="candidate_terminal_failure_authority_invalid",
            )
        raise CandidateAuthorityUnavailable(
            "candidate Core Attempt reached a terminal failure",
            reason_code=(
                lifecycle.error_code
                or (
                    failure.failure_code
                    if failure is not None
                    else "candidate_cancelled"
                )
            ),
            terminal_proven=True,
            failure_receipt={
                "schema_version": "openevo.candidate_terminal_failure.v1",
                "core_task_id": task_id,
                "core_attempt_id": attempt_id,
                "underlying_session_status": (
                    failure.session_status if failure is not None else "CANCELLED"
                ),
                "model_started": lifecycle.model_started,
                "benchmark_started": lifecycle.benchmark_started,
                "retryable": lifecycle.retryable,
                "core_failure_code": (
                    lifecycle.error_code
                    or (failure.failure_code if failure is not None else None)
                ),
                "terminal_proven": True,
            },
        )
    return lifecycle.state == "captured"


class _AuthorityJournal:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _path(self, key: str) -> Path:
        return self.root / f"{hashlib.sha256(key.encode()).hexdigest()}.json"

    def read(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def write(self, key: str, value: dict[str, Any]) -> None:
        path = self._path(key)
        prior = self.read(key)
        if prior == value:
            return
        if prior is not None:
            raise ValueError("production authority journal conflict")
        atomic_write_json(path, value)


def _project_config(config: ExperimentConfig, task_id: str, objective: str) -> ScienceProjectConfigV2:
    targets = {
        artifact_type: {
            "enabled": True,
            "method": NATIVE_METHODS[artifact_type],
            # Stop successor preparation after the candidate dataset is
            # sealed until the trusted evaluator attachment exists.  This
            # project-scoped gate is part of each reflector method's verified
            # closed configuration and prevents pre-evaluator evolution.
            "config": {"training_feedback_required": True},
        }
        for artifact_type in ARTIFACT_TYPES
    }
    targets["parametric_memory"] = {"enabled": False, "method": None, "config": {}}
    return ScienceProjectConfigV2.model_validate(
        {
            "task": {"title": task_id, "objective": objective},
            "workspace": {"kind": "native_folder_snapshot", "display_name": f"{task_id} sanitized workspace"},
            "execution": {
                "mode": "codex_subscription_transcript",
                "capture_mode": "transcript",
                "token_level_metrics_available": False,
                "harness_id": "codex",
                "codex_model": config.require("candidate.model"),
                "reasoning_effort": config.require("candidate.reasoning_level"),
                "token_limit": config.require("candidate.token_limit"),
                "task_network_allow_internet": False,
            },
            "evolution": {"targets": targets},
        }
    )


def _compose_candidate_objective(
    instructions: Path,
    *,
    task_local_overlay: dict[str, Any] | None,
) -> str:
    """Compose the exact objective used by the production Candidate port."""

    overlay_text = None
    if task_local_overlay is not None:
        overlay_text = json.dumps(
            task_local_overlay,
            ensure_ascii=True,
            sort_keys=True,
            allow_nan=False,
        )
    return compose_native_instruction(
        instructions,
        task_local_overlay=overlay_text,
    ).text


def _require_project_capabilities(
    capabilities_payload: dict[str, Any],
    project_config: ScienceProjectConfigV2,
    *,
    expected_registry_digest: str,
) -> None:
    """Validate exact native method selections before persisting an intent."""

    capabilities = CapabilitiesResponseV2.model_validate(capabilities_payload)
    if capabilities.registry_digest != expected_registry_digest:
        raise CoreControlError(
            "CORE_CAPABILITY_REGISTRY_MISMATCH",
            reason_code="core_capability_registry_mismatch",
        )
    targets = {target.target_id: target for target in capabilities.targets}
    for target_id, selection in project_config.evolution.targets.items():
        if not selection.enabled:
            continue
        target = targets.get(target_id)
        if target is None:
            raise CoreControlError(
                "CORE_CAPABILITY_TARGET_UNAVAILABLE",
                reason_code="core_capability_target_unavailable",
            )
        method = next(
            (item for item in target.methods if item.method_id == selection.method),
            None,
        )
        if method is None or method.support.overall.value != "supported":
            raise CoreControlError(
                "CORE_CAPABILITY_METHOD_UNAVAILABLE",
                reason_code="core_capability_method_unavailable",
            )
        try:
            normalize_config_override(
                json.loads(method.config_schema_json),
                json.loads(method.default_config_json),
                selection.config.to_dict(),
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CoreControlError(
                "CORE_CAPABILITY_METHOD_CONFIG_INVALID",
                reason_code="core_capability_method_config_invalid",
            ) from exc


def _safe_export_result(payload: bytes, destination: Path, expected_sha256: str) -> None:
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise ValueError("Core workspace result archive hash changed")
    allowed_roots = {"code", "outputs", "report"}
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
        for member in archive.getmembers():
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
                raise ValueError("Core workspace result contains an unsafe path")
            if relative.parts[0] not in allowed_roots:
                continue
            target = destination.joinpath(*relative.parts)
            if not target.resolve(strict=False).is_relative_to(destination):
                raise ValueError("Core workspace result escapes official run root")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile() or member.issym() or member.islnk():
                raise ValueError("Core workspace result contains a non-regular deliverable")
            source = archive.extractfile(member)
            if source is None:
                raise ValueError("Core workspace result member is unavailable")
            data = source.read()
            if len(data) != member.size:
                raise ValueError("Core workspace result member is truncated")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if target.is_symlink() or not target.is_file() or target.read_bytes() != data:
                    raise ValueError("recovered Core workspace result conflicts with sealed output")
                continue
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
            try:
                os.write(descriptor, data)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)


def _workspace_archive_declaration(root: Path) -> WorkspaceArchiveDeclarationV2:
    """Recompute one candidate-visible tree without retaining a second archive."""

    assert_candidate_workspace_shape(root)
    verification = root.parent / f".{root.name}.{secrets.token_hex(8)}.verify.tar"
    descriptor = os.open(
        verification,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        return write_workspace_archive(root, descriptor)
    finally:
        os.close(descriptor)
        verification.unlink(missing_ok=True)


def _materialize_successor_workspace(
    *,
    payload: bytes,
    destination: Path,
    expected_archive: dict[str, Any],
    runtime_injection_receipt: dict[str, Any] | None = None,
) -> tuple[WorkspaceArchiveDeclarationV2, dict[str, Any]]:
    """Materialize a sealed Core result as the next attempt's immutable input.

    The downloaded archive is already covered by the sealed SessionResult.  We
    still validate every member, close the visible root set, and deterministically
    re-archive the materialized tree before publishing it with an atomic rename.
    This gives the next attempt a local authority for the exact workspace that
    the successor transition will place in the Core project head.
    """

    expected = WorkspaceArchiveDeclarationV2.model_validate(expected_archive)
    if (
        hashlib.sha256(payload).hexdigest() != expected.content_sha256
        or len(payload) != expected.byte_size
    ):
        raise ValueError("sealed successor workspace archive changed")
    destination = destination.resolve(strict=False)
    destination_preexisting = destination.exists()
    if destination_preexisting:
        if destination.is_symlink() or not destination.is_dir():
            raise ValueError("successor workspace destination is unsafe")
        temporary = destination
    else:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if destination.parent.is_symlink():
            raise ValueError("successor workspace parent is a symlink")
        temporary = destination.with_name(f".{destination.name}.building")
        if temporary.exists():
            raise FileExistsError(
                f"unfinished successor workspace materialization exists: {temporary}"
            )
        temporary.mkdir(mode=0o700)
    runtime_agent_system: dict[str, Any] | None = None
    if runtime_injection_receipt is not None:
        files = runtime_injection_receipt.get("files")
        matching = (
            [
                item
                for item in files
                if isinstance(item, dict)
                and item.get("relative_path") == "agent_system_targets/AGENTS.md"
            ]
            if isinstance(files, list)
            else []
        )
        if len(matching) != 1:
            raise ValueError("successor workspace runtime receipt is invalid")
        runtime_agent_system = matching[0]
        if (
            set(runtime_agent_system) != {"relative_path", "size_bytes", "sha256"}
            or isinstance(runtime_agent_system.get("size_bytes"), bool)
            or not isinstance(runtime_agent_system.get("size_bytes"), int)
            or runtime_agent_system["size_bytes"] < 0
            or not isinstance(runtime_agent_system.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", runtime_agent_system["sha256"])
            is None
        ):
            raise ValueError("successor workspace runtime receipt is invalid")

    seen: set[str] = set()
    extracted_bytes = 0
    stripped_bytes = 0
    stripped_entries = 0
    stripped_projection_entries: list[dict[str, Any]] = []
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
        members = archive.getmembers()
        if len(members) != expected.entry_count:
            raise ValueError("successor workspace entry count changed")
        for member in members:
            relative = PurePosixPath(member.name)
            if (
                relative.is_absolute()
                or not relative.parts
                or any(part in {"", ".", ".."} for part in relative.parts)
                or relative.as_posix() in seen
            ):
                raise ValueError("successor workspace contains an unsafe path")
            seen.add(relative.as_posix())
            if relative.parts[0] not in CANDIDATE_VISIBLE_ENTRIES:
                # The managed Candidate runtime creates these empty
                # top-level scaffolding directories even for the c000 genesis
                # head, where no evolved agent-system receipt exists.  They
                # carry no content and are never part of the successor input.
                # Any nested entry remains outside CANDIDATE_VISIBLE_ENTRIES
                # and is rejected below on its own member record.
                if (
                    len(relative.parts) == 1
                    and relative.name in {".agents", ".codex", ".git"}
                    and member.isdir()
                    and member.size == 0
                ):
                    stripped_entries += 1
                    stripped_projection_entries.append(
                        {
                            "kind": "empty_runtime_scaffold",
                            "relative_path": relative.name,
                            "size_bytes": 0,
                        }
                    )
                    continue
                if (
                    runtime_agent_system is None
                    or len(relative.parts) != 1
                    or relative.name != "AGENTS.md"
                ):
                    raise ValueError("successor workspace contains an unsafe path")
                if (
                    not member.isfile()
                    or member.issym()
                    or member.islnk()
                    or member.size != runtime_agent_system["size_bytes"]
                ):
                    raise ValueError("successor workspace contains an unsafe path")
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError("successor workspace member is unavailable")
                data = source.read()
                if (
                    len(data) != member.size
                    or hashlib.sha256(data).hexdigest()
                    != runtime_agent_system["sha256"]
                ):
                    raise ValueError("successor workspace contains an unsafe path")
                stripped_entries += 1
                stripped_bytes += len(data)
                stripped_projection_entries.append(
                    {
                        "kind": "runtime_agent_system",
                        "relative_path": "AGENTS.md",
                        "runtime_receipt_path": (
                            "agent_system_targets/AGENTS.md"
                        ),
                        "sha256": runtime_agent_system["sha256"],
                        "size_bytes": len(data),
                    }
                )
                continue
            target = temporary.joinpath(*relative.parts)
            if not target.resolve(strict=False).is_relative_to(temporary):
                raise ValueError("successor workspace path escapes materialization")
            if member.isdir():
                if destination_preexisting:
                    if (
                        target.is_symlink()
                        or not target.is_dir()
                        or target.stat().st_mode & 0o777 != 0o700
                    ):
                        raise ValueError("materialized successor workspace drifted")
                else:
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                    os.chmod(target, 0o700)
                continue
            if not member.isfile() or member.issym() or member.islnk():
                raise ValueError("successor workspace contains a non-regular entry")
            source = archive.extractfile(member)
            if source is None:
                raise ValueError("successor workspace member is unavailable")
            data = source.read()
            if len(data) != member.size:
                raise ValueError("successor workspace member is truncated")
            expected_mode = 0o700 if member.mode & 0o111 else 0o600
            if destination_preexisting:
                if (
                    target.is_symlink()
                    or not target.is_file()
                    or target.stat().st_mode & 0o777 != expected_mode
                    or target.read_bytes() != data
                ):
                    raise ValueError("materialized successor workspace drifted")
            else:
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                descriptor = os.open(
                    target,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | os.O_CLOEXEC
                    | os.O_NOFOLLOW,
                    expected_mode,
                )
                try:
                    view = memoryview(data)
                    while view:
                        written = os.write(descriptor, view)
                        if written <= 0:
                            raise OSError("successor workspace write did not advance")
                        view = view[written:]
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            extracted_bytes += len(data)
    if extracted_bytes + stripped_bytes != expected.extracted_byte_size:
        raise ValueError("successor workspace extracted byte count changed")
    assert_candidate_workspace_shape(temporary)
    observed = _workspace_archive_declaration(temporary)
    if stripped_entries:
        if (
            observed.entry_count != expected.entry_count - stripped_entries
            or observed.extracted_byte_size
            != expected.extracted_byte_size - stripped_bytes
        ):
            raise ValueError("successor workspace canonical archive changed")
    elif observed != expected:
        raise ValueError("successor workspace canonical archive changed")
    projection = _successor_workspace_projection_authority(
        core_archive=expected,
        sanitized_archive=observed,
        stripped_entries=stripped_projection_entries,
    )
    if not destination_preexisting:
        temporary.rename(destination)
    return observed, projection


def _require_successor_workspace_authority(
    *,
    authority: object,
    run_root: Path,
    task_id: str,
    attempt_index: int,
    project_id: str,
) -> tuple[Path, WorkspaceArchiveDeclarationV2]:
    """Resolve only the preceding same-task sealed workspace authority."""

    required = {
        "schema_version",
        "task_id",
        "source_attempt_index",
        "source_run_id",
        "project_id",
        "input_project_head_id",
        "successor_transition_id",
        "workspace_handoff_id",
        "session_id",
        "dataset_id",
        "session_result_sha256",
        "workspace_root",
        "output_archive",
        "core_output_archive",
        "workspace_projection",
        "content_sha256",
    }
    if not isinstance(authority, dict) or set(authority) != required:
        raise CoreControlError(
            "CANDIDATE_SUCCESSOR_WORKSPACE_AUTHORITY_INVALID",
            reason_code="candidate_successor_workspace_authority_invalid",
        )
    closed = {key: authority[key] for key in authority if key != "content_sha256"}
    scalar_fields = (
        "source_run_id",
        "project_id",
        "input_project_head_id",
        "successor_transition_id",
        "workspace_handoff_id",
        "session_id",
        "dataset_id",
    )
    if (
        authority.get("schema_version")
        != "openevo.researchclawbench.successor_workspace_authority.v2"
        or authority.get("task_id") != task_id
        or authority.get("source_attempt_index") != attempt_index - 1
        or attempt_index not in {1, 2}
        or authority.get("project_id") != project_id
        or not all(
            isinstance(authority.get(field), str) and authority[field]
            for field in scalar_fields
        )
        or not str(authority["source_run_id"]).startswith(
            f"{task_id}_a{attempt_index - 1}_"
        )
        or re.fullmatch(r"[0-9a-f]{64}", str(authority["session_result_sha256"]))
        is None
        or authority.get("content_sha256") != canonical_sha256(closed)
    ):
        raise CoreControlError(
            "CANDIDATE_SUCCESSOR_WORKSPACE_AUTHORITY_INVALID",
            reason_code="candidate_successor_workspace_authority_invalid",
        )
    try:
        raw_root = Path(str(authority["workspace_root"]))
        if raw_root.is_symlink():
            raise ValueError("successor workspace root is a symlink")
        root = raw_root.resolve(strict=True)
        allowed = (run_root / "successor_workspaces").resolve(strict=True)
        expected_archive = WorkspaceArchiveDeclarationV2.model_validate(
            authority["output_archive"]
        )
        core_archive = WorkspaceArchiveDeclarationV2.model_validate(
            authority["core_output_archive"]
        )
        _require_successor_workspace_projection(
            authority=authority["workspace_projection"],
            core_archive=core_archive,
            sanitized_archive=expected_archive,
        )
    except (OSError, TypeError, ValueError) as exc:
        raise CoreControlError(
            "CANDIDATE_SUCCESSOR_WORKSPACE_AUTHORITY_INVALID",
            reason_code="candidate_successor_workspace_authority_invalid",
        ) from exc
    if root.is_symlink() or not root.is_dir() or not root.is_relative_to(allowed):
        raise CoreControlError(
            "CANDIDATE_SUCCESSOR_WORKSPACE_AUTHORITY_INVALID",
            reason_code="candidate_successor_workspace_authority_invalid",
        )
    try:
        observed = _workspace_archive_declaration(root)
    except (OSError, ValueError) as exc:
        raise CoreControlError(
            "CANDIDATE_SUCCESSOR_WORKSPACE_AUTHORITY_DRIFT",
            reason_code="candidate_successor_workspace_authority_drift",
        ) from exc
    if observed != expected_archive:
        raise CoreControlError(
            "CANDIDATE_SUCCESSOR_WORKSPACE_AUTHORITY_DRIFT",
            reason_code="candidate_successor_workspace_authority_drift",
        )
    return root, observed


def _require_successor_core_workspace_projection(
    *,
    authority: object,
    sanitized_archive: WorkspaceArchiveDeclarationV2,
) -> tuple[WorkspaceArchiveDeclarationV2, dict[str, Any]]:
    """Resolve the Core archive paired with a verified local projection.

    The full successor receipt is validated while selecting the local
    workspace.  This helper closes the second half of that receipt at the
    project-head comparison sites without treating the sanitized archive as
    the Core-owned snapshot.
    """

    if not isinstance(authority, dict):
        raise CoreControlError(
            "CANDIDATE_SUCCESSOR_WORKSPACE_AUTHORITY_INVALID",
            reason_code="candidate_successor_workspace_authority_invalid",
        )
    try:
        if authority.get("output_archive") != sanitized_archive.model_dump(
            mode="json"
        ):
            raise ValueError("sanitized successor archive changed")
        core_archive = WorkspaceArchiveDeclarationV2.model_validate(
            authority["core_output_archive"]
        )
        projection = _require_successor_workspace_projection(
            authority=authority["workspace_projection"],
            core_archive=core_archive,
            sanitized_archive=sanitized_archive,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CoreControlError(
            "CANDIDATE_SUCCESSOR_WORKSPACE_AUTHORITY_INVALID",
            reason_code="candidate_successor_workspace_authority_invalid",
        ) from exc
    return core_archive, projection


def _closed_task_local_overlay(
    attachment: dict[str, Any],
    *,
    task_id: str,
    core_task_scope_id: str,
) -> tuple[dict[str, Any], str]:
    task_local = attachment.get("task_local_feedback")
    if (
        attachment.get("status") != "sealed"
        or attachment.get("task_scope_id") != core_task_scope_id
        or not isinstance(task_local, dict)
        or not task_local
        or task_local.get("benchmark_task_scope_id") != task_id
    ):
        raise ValueError("task-local overlay lacks same-task sealed Core authority")
    closed = json.loads(canonical_bytes(task_local))
    closed.pop("benchmark_task_scope_id")
    return closed, canonical_sha256(closed)


def _require_selected_core_head(
    composites: LocalCompositePort,
    *,
    selected_composite_id: str,
    active_project_head: dict[str, Any] | None,
) -> None:
    if active_project_head is None or selected_composite_id == "c000":
        return
    selected = composites.get_composite(selected_composite_id)
    if selected.get("core_project_head_id") != active_project_head.get("project_head_id"):
        raise ValueError(
            "selected composite is not the active Core project head; "
            "a native historical restore is required before candidate execution"
        )


def _candidate_runtime_injection_authority(
    *,
    session_result: dict[str, Any],
    project_head: dict[str, Any],
    selected_composite_id: str,
    selected_composite: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return a closed receipt proving the exact admitted runtime composition."""

    session = SessionResult.model_validate(session_result)
    head = ProjectHeadRefV2.model_validate(project_head)
    metadata = session.metadata
    openevo = metadata.get("openevo")
    evolution = metadata.get("evolution")
    if not isinstance(openevo, dict) or not isinstance(evolution, dict):
        raise ValueError("sealed SessionResult lacks runtime injection metadata")
    if (
        openevo.get("project_id") != head.project_id
        or openevo.get("project_head_id") != head.project_head_id
        or openevo.get("evolution_revision_id")
        != head.evolution_revision.evolution_revision_id
        or openevo.get("runtime_context_snapshot_id")
        != head.runtime_context_snapshot.runtime_context_snapshot_id
    ):
        raise ValueError("sealed SessionResult runtime identity differs from Core head")

    # Core v2 compiles committed runtime context from the project-head binding,
    # so the caller-supplied context inventory is intentionally absent from a
    # sealed SessionResult.  The managed Gateway receipt remains the durable
    # authority in that projection.  If an inventory is present, however, it
    # is still untrusted input and must agree exactly with that receipt.
    raw_context_ids = evolution.get("context_artifact_ids")
    context_ids: list[str] | None = None
    if raw_context_ids is not None:
        if (
            not isinstance(raw_context_ids, list)
            or not all(isinstance(item, str) and item for item in raw_context_ids)
            or len(raw_context_ids) != len(set(raw_context_ids))
        ):
            raise ValueError("sealed SessionResult context artifact inventory is invalid")
        context_ids = list(raw_context_ids)
    receipt = evolution.get("runtime_injection_receipt")
    if selected_composite_id == "c000":
        if (
            (selected_composite is not None and selected_composite != {})
            or head.generation != 0
            or head.predecessor_project_head_id is not None
            or head.evolution_revision.artifact_count != 0
            or context_ids
            or evolution.get("context_injected") is not False
            or evolution.get("context_source") != "empty_genesis"
            or receipt is not None
        ):
            raise ValueError("c000 candidate did not execute an empty generation-zero head")
        authority = {
            "schema_version": "openevo.candidate_runtime_injection.v1",
            "selected_composite_id": "c000",
            "project_head_id": head.project_head_id,
            "generation": 0,
            "predecessor_project_head_id": None,
            "evolution_revision_id": head.evolution_revision.evolution_revision_id,
            "artifact_count": 0,
            "artifact_ids": [],
            "runtime_injection_receipt": None,
        }
        return {**authority, "content_sha256": canonical_sha256(authority)}

    registry_artifacts = (
        selected_composite.get("registry_artifacts")
        if isinstance(selected_composite, dict)
        else None
    )
    if (
        not isinstance(registry_artifacts, dict)
        or set(registry_artifacts) != set(ARTIFACT_TYPES)
        or head.generation == 0
        or head.predecessor_project_head_id is None
        or head.evolution_revision.artifact_count != 3
        or evolution.get("context_injected") is not True
        or evolution.get("context_source")
        not in {"materialized_successor", "materialized_inherited"}
        or not isinstance(receipt, dict)
    ):
        raise ValueError("candidate lacks exact triple-artifact runtime injection")
    expected_by_type: dict[str, str] = {}
    for artifact_type, value in registry_artifacts.items():
        registry_id = value.get("registry_id") if isinstance(value, dict) else None
        if not isinstance(registry_id, str) or not registry_id:
            raise ValueError("selected composite registry authority is incomplete")
        expected_by_type[artifact_type] = registry_id

    required_receipt_fields = {
        "schema_version",
        "context_id",
        "context_manifest_sha256",
        "revision_id",
        "runtime_context_snapshot_id",
        "project_head_id",
        "instruction_sha256",
        "runtime_tree_sha256",
        "files",
        "artifacts",
    }
    files = receipt.get("files")
    artifacts = receipt.get("artifacts")
    if (
        set(receipt) != required_receipt_fields
        or receipt.get("schema_version") != "4"
        or receipt.get("context_id") != evolution.get("context_id")
        or receipt.get("revision_id")
        != head.evolution_revision.evolution_revision_id
        or receipt.get("runtime_context_snapshot_id")
        != head.runtime_context_snapshot.runtime_context_snapshot_id
        or receipt.get("project_head_id") != head.project_head_id
        or not all(
            isinstance(receipt.get(name), str)
            and len(receipt[name]) == 64
            and all(character in "0123456789abcdef" for character in receipt[name])
            for name in (
                "context_manifest_sha256",
                "instruction_sha256",
                "runtime_tree_sha256",
            )
        )
        or not isinstance(files, list)
        or not 0 < len(files) <= 4096
        or not isinstance(artifacts, list)
        or len(artifacts) != 3
    ):
        raise ValueError("candidate runtime injection receipt is invalid")

    validated_files: list[dict[str, Any]] = []
    total_bytes = 0
    for item in files:
        path = item.get("relative_path") if isinstance(item, dict) else None
        size = item.get("size_bytes") if isinstance(item, dict) else None
        digest = item.get("sha256") if isinstance(item, dict) else None
        parsed = PurePosixPath(path) if isinstance(path, str) else None
        if (
            not isinstance(item, dict)
            or set(item) != {"relative_path", "size_bytes", "sha256"}
            or parsed is None
            or parsed.is_absolute()
            or path != parsed.as_posix()
            or any(part in {"", ".", ".."} for part in parsed.parts)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("candidate runtime injection file receipt is invalid")
        total_bytes += size
        validated_files.append(dict(item))
    paths = [item["relative_path"] for item in validated_files]
    files_by_path = {item["relative_path"]: item for item in validated_files}
    if (
        total_bytes > 128 * 1024 * 1024
        or paths != sorted(paths)
        or len(paths) != len(set(paths))
        or receipt["runtime_tree_sha256"]
        != canonical_sha256({"files": validated_files})
        or files_by_path.get("evolution/instruction.txt", {}).get("sha256")
        != receipt["instruction_sha256"]
    ):
        raise ValueError("candidate runtime injection file authority is not canonical")

    observed_by_type: dict[str, str] = {}
    validated_artifacts: list[dict[str, Any]] = []
    for item in artifacts:
        if not isinstance(item, dict) or set(item) != {
            "artifact_id",
            "artifact_type",
            "content_sha256",
            "runtime_paths",
            "runtime_tree_sha256",
        }:
            raise ValueError("candidate runtime injection artifact receipt is invalid")
        artifact_type = item.get("artifact_type")
        artifact_id = item.get("artifact_id")
        runtime_paths = item.get("runtime_paths")
        if (
            artifact_type not in ARTIFACT_TYPES
            or not isinstance(artifact_id, str)
            or not artifact_id
            or not isinstance(runtime_paths, list)
            or not runtime_paths
            or runtime_paths != sorted(runtime_paths)
            or len(runtime_paths) != len(set(runtime_paths))
            or not all(isinstance(path, str) and path in files_by_path for path in runtime_paths)
            or not all(
                isinstance(item.get(name), str)
                and len(item[name]) == 64
                and all(character in "0123456789abcdef" for character in item[name])
                for name in ("content_sha256", "runtime_tree_sha256")
            )
            or item["runtime_tree_sha256"]
            != canonical_sha256(
                {"files": [files_by_path[path] for path in runtime_paths]}
            )
            or artifact_type in observed_by_type
        ):
            raise ValueError("candidate runtime injection artifact authority is invalid")
        observed_by_type[artifact_type] = artifact_id
        validated_artifacts.append(dict(item))
    if observed_by_type != expected_by_type:
        raise ValueError("candidate runtime injection differs from selected composite")
    if context_ids is not None and context_ids != [
        item["artifact_id"] for item in artifacts
    ]:
        raise ValueError("candidate context metadata differs from injection receipt")

    normalized_receipt = {**receipt, "files": validated_files, "artifacts": validated_artifacts}
    authority = {
        "schema_version": "openevo.candidate_runtime_injection.v1",
        "selected_composite_id": selected_composite_id,
        "project_head_id": head.project_head_id,
        "generation": head.generation,
        "predecessor_project_head_id": head.predecessor_project_head_id,
        "evolution_revision_id": head.evolution_revision.evolution_revision_id,
        "artifact_count": 3,
        "artifact_ids": [item["artifact_id"] for item in validated_artifacts],
        "runtime_injection_receipt": normalized_receipt,
        "runtime_injection_receipt_sha256": canonical_sha256(normalized_receipt),
    }
    return {**authority, "content_sha256": canonical_sha256(authority)}


def _wait_for_completed_dataset(
    client: CoreControlV2Client,
    *,
    task_id: str,
    attempt_id: str,
    deadline: float,
    pre_dataset_retry_idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Resolve one attempt's durable dataset across the publication race.

    A captured Candidate is already a completed paid side effect.  Core starts
    its successor immediately afterwards so the transcript dataset can be
    published before trusted evaluator feedback is attached.  If that first
    successor attempt fails *before* ``dataset_sealed`` with explicit
    retryable authority, retry only that transition once under the Candidate
    operation's deterministic idempotency key.  The project requires training
    feedback, so this retry can publish the dataset but cannot start reflector
    methods before the evaluator attachment exists.
    """

    pre_dataset_retry_attempted = False
    while time.monotonic() < deadline:
        timeline = client.json("GET", f"/v2/tasks/{task_id}/timeline?limit=100")
        sealed = [
            item
            for item in timeline.get("items", [])
            if item.get("event_type") == "dataset_sealed"
            and item.get("attempt_id") == attempt_id
        ]
        if len(sealed) > 1:
            raise CoreControlError(
                "Core published multiple completed datasets for one attempt"
            )
        if len(sealed) == 1:
            try:
                dataset = client.json(
                    "GET",
                    "/v2/internal/training-feedback/datasets/"
                    + str(sealed[0]["dataset_id"]),
                )
            except CoreControlError as exc:
                if exc.status_code != 404:
                    raise
                dataset = None
            if dataset is not None:
                return dataset
        task = client.json(
            "GET",
            f"/v2/tasks/{quote(task_id, safe='')}",
        )
        if task.get("state") in {"failed", "cancelled"}:
            successor = task.get("successor_transition")
            transition_id = (
                successor.get("successor_transition_id")
                if isinstance(successor, dict)
                else None
            )
            if isinstance(transition_id, str) and transition_id:
                authority = client.json(
                    "GET",
                    "/v2/internal/training-successors/"
                    + quote(transition_id, safe=""),
                )
                transition = authority.get("transition")
                if (
                    isinstance(transition, dict)
                    and transition.get("state") == "failed"
                ):
                    failure = transition.get("error")
                    predecessor = transition.get("transition", {}).get(
                        "predecessor_project_head"
                    )
                    if (
                        pre_dataset_retry_idempotency_key is not None
                        and not pre_dataset_retry_attempted
                        and isinstance(failure, dict)
                        and failure.get("retryable") is True
                        and isinstance(predecessor, dict)
                        and isinstance(predecessor.get("project_head_id"), str)
                        and predecessor["project_head_id"]
                    ):
                        client.json(
                            "POST",
                            "/v2/transitions/"
                            + quote(transition_id, safe="")
                            + "/retry",
                            payload={
                                "expected_project_head_id": predecessor[
                                    "project_head_id"
                                ]
                            },
                            headers={
                                "Idempotency-Key": (
                                    pre_dataset_retry_idempotency_key
                                )
                            },
                        )
                        pre_dataset_retry_attempted = True
                        continue
                    raise CoreControlError(
                        (
                            "Core successor retry failed before dataset "
                            "publication"
                            if pre_dataset_retry_attempted
                            else "Core successor reached a terminal failure "
                            "before dataset publication"
                        ),
                        reason_code=(
                            "candidate_successor_retry_failed_before_dataset"
                            if pre_dataset_retry_attempted
                            else "candidate_successor_transition_failed"
                        ),
                    )
            raise CoreControlError(
                "Core task terminated before dataset publication",
                reason_code="candidate_dataset_publication_terminated",
            )
        time.sleep(1.0)
    raise TimeoutError("Core did not publish the completed dataset authority")


class CoreV2CandidatePort(ProductionOperationPort):
    """Fresh workspace -> Core v2 Task -> captured Session/dataset authority."""

    def __init__(
        self,
        config: ExperimentConfig,
        run_root: Path,
        *,
        core_authority: ManagedCoreControlAuthority,
        composites: LocalCompositePort,
    ) -> None:
        self.config = config
        self.run_root = run_root
        self.core_authority = core_authority
        self.composites = composites
        self.journal = _AuthorityJournal(run_root / "core_candidate_authority")

    @contextmanager
    def _client(self) -> Iterator[CoreControlV2Client]:
        client = CoreControlV2Client(self.core_authority)
        try:
            yield client
        finally:
            client.close()

    def _candidate_workspace(self, request: dict[str, Any]) -> WorkspaceReceipt:
        task_id = str(request["task_id"])
        run_id = str(request["run_id"])
        attempt_index = int(request["attempt_index"])
        workspace_root = self.run_root / "runs" / run_id
        if not workspace_root.exists():
            workspace = build_official_workspace(
                self.config.researchclawbench_root,
                task_id,
                self.run_root / "runs",
                run_id,
            )
        else:
            candidate_root = self.run_root / "sanitized_workspaces" / task_id
            workspace = WorkspaceReceipt(
                task_id=task_id,
                run_id=run_id,
                workspace=workspace_root,
                candidate_workspace=candidate_root,
                instructions=workspace_root / "INSTRUCTIONS.md",
                data=workspace_root / "data",
                related_work=workspace_root / "related_work",
                code=workspace_root / "code",
                outputs=workspace_root / "outputs",
                report=workspace_root / "report",
            )
            if not workspace.instructions.is_file() or not candidate_root.is_dir():
                raise ValueError("candidate recovery workspace authority is incomplete")

        successor = request.get("successor_workspace_authority")
        preseeded = request.get("preseeded_workspace_authority")
        if successor is not None and preseeded is not None:
            raise CoreControlError(
                "CANDIDATE_WORKSPACE_AUTHORITY_AMBIGUOUS",
                reason_code="candidate_workspace_authority_ambiguous",
            )
        if successor is not None:
            project_id = request.get("core_project_id")
            if not isinstance(project_id, str) or not project_id:
                raise CoreControlError(
                    "CANDIDATE_SUCCESSOR_WORKSPACE_AUTHORITY_INVALID",
                    reason_code="candidate_successor_workspace_authority_invalid",
                )
            candidate_root, _archive = _require_successor_workspace_authority(
                authority=successor,
                run_root=self.run_root,
                task_id=task_id,
                attempt_index=attempt_index,
                project_id=project_id,
            )
            workspace = WorkspaceReceipt(
                task_id=workspace.task_id,
                run_id=workspace.run_id,
                workspace=workspace.workspace,
                candidate_workspace=candidate_root,
                instructions=workspace.instructions,
                data=workspace.data,
                related_work=workspace.related_work,
                code=workspace.code,
                outputs=workspace.outputs,
                report=workspace.report,
            )
        elif attempt_index in {1, 2} and preseeded is None:
            raise CoreControlError(
                "CANDIDATE_SUCCESSOR_WORKSPACE_AUTHORITY_MISSING",
                reason_code="candidate_successor_workspace_authority_missing",
            )
        assert_candidate_workspace_shape(workspace.candidate_workspace)
        return workspace

    def _preflight_workspace_binding(
        self,
        client: CoreControlV2Client,
        request: dict[str, Any],
    ) -> dict[str, Any] | None:
        workspace = self._candidate_workspace(request)
        project_id = request.get("core_project_id")
        if project_id is None:
            return None
        project = client.json("GET", f"/v2/projects/{project_id}")
        if project.get("project_id") != project_id:
            raise CoreControlError(
                "CANDIDATE_PROJECT_AUTHORITY_DRIFT",
                reason_code="candidate_project_authority_drift",
            )
        project_head = project.get("active_project_head")
        if not isinstance(project_head, dict):
            raise CoreControlError(
                "CANDIDATE_PROJECT_HEAD_MISSING",
                reason_code="candidate_project_head_missing",
            )
        _require_selected_core_head(
            self.composites,
            selected_composite_id=str(request["input_composite_id"]),
            active_project_head=project_head,
        )
        archive = _workspace_archive_declaration(workspace.candidate_workspace)
        if request.get("preseeded_workspace_authority") is not None:
            core_archive = archive
            workspace_projection = None
            expected_snapshot = _require_preseeded_workspace_authority(
                authority=request["preseeded_workspace_authority"],
                project=project,
                project_head=project_head,
                archive=archive,
            )
            source = "recovery_preseed"
        else:
            successor_authority = request.get("successor_workspace_authority")
            if successor_authority is None:
                core_archive = archive
                workspace_projection = None
            else:
                (
                    core_archive,
                    workspace_projection,
                ) = _require_successor_core_workspace_projection(
                    authority=successor_authority,
                    sanitized_archive=archive,
                )
            expected_snapshot = _require_existing_workspace_snapshot(
                project=project,
                project_head=project_head,
                archive=core_archive,
            )
            source = (
                "successor_workspace"
                if successor_authority is not None
                else "cross_task_genesis"
            )
        binding = _candidate_workspace_binding_receipt(
            task_id=str(request["task_id"]),
            attempt_index=int(request["attempt_index"]),
            workspace_source=source,
            workspace_root=workspace.candidate_workspace,
            project=project,
            project_head=project_head,
            archive=archive,
            core_archive=core_archive,
            workspace_projection=workspace_projection,
            expected_snapshot=expected_snapshot,
            task_local_overlay_id=request.get("task_local_overlay_id"),
        )
        if binding["workspace_snapshot_match"] is not True:
            raise CoreControlError(
                "CANDIDATE_WORKSPACE_BINDING_DRIFT",
                reason_code="candidate_workspace_binding_drift",
            )
        return binding

    def preflight(self, request: dict[str, Any]) -> dict[str, Any]:
        if not self.core_authority.managed_host_profile_ready:
            raise CoreControlError(
                "MANAGED_CORE_HOST_PROFILE_UNAVAILABLE",
                reason_code="managed_core_host_profile_unavailable",
            )
        with self._client() as client:
            readiness = client.readiness()
            capabilities = client.json(
                "GET",
                "/v2/capabilities?execution_mode=codex_subscription_transcript",
            )
            candidate_runtime = client.json(
                "GET",
                "/v2/internal/managed-candidate/readiness",
            )
            workspace_binding = None
            if {
                "run_id",
                "attempt_index",
                "fresh_workspace",
                "resume_in_place",
                "input_composite_id",
            }.issubset(request):
                workspace_binding = self._preflight_workspace_binding(
                    client,
                    request,
                )
        _require_project_capabilities(
            capabilities,
            _project_config(
                self.config,
                str(request["task_id"]),
                "Candidate project capability preflight.",
            ),
            expected_registry_digest=self.core_authority.registry_digest,
        )
        expected_generation = request.get("core_control_generation")
        if expected_generation is not None and expected_generation != readiness["generation"]:
            raise CoreControlError("CORE_CONTROL_GENERATION_MISMATCH")
        expected_release = request.get("core_control_release_identity")
        if expected_release is not None and expected_release != readiness["release_identity"]:
            raise CoreControlError("CORE_CONTROL_RELEASE_MISMATCH")
        runtime_receipt = require_managed_candidate_isolation_readiness(
            candidate_runtime=candidate_runtime,
            core_readiness=readiness,
            expected_registry_digest=self.core_authority.registry_digest,
        )
        result = {
            **readiness,
            "managed_candidate_runtime": runtime_receipt,
            "managed_candidate_runtime_ready": True,
            "managed_candidate_credential_mount_adopted": True,
            "managed_candidate_codex_cli_version": "0.144.1",
            "managed_candidate_model_started": False,
            "managed_candidate_subscription_isolation": {
                "authority_issued": runtime_receipt["authority_issued"],
                "mount_adopted": runtime_receipt["adoption_verified"],
                "codex_cli_auth_visible": runtime_receipt[
                    "codex_cli_auth_visible"
                ],
                "host_source_hidden": runtime_receipt["host_source_hidden"],
                "tool_sandbox_credential_hidden": runtime_receipt[
                    "tool_sandbox_credential_hidden"
                ],
                "tool_environment_clean": runtime_receipt[
                    "tool_environment_clean"
                ],
                "parent_process_secret_unreadable": runtime_receipt[
                    "parent_process_secret_unreadable"
                ],
                "generation_matches": runtime_receipt["generation_matches"],
                "release_identity_matches": True,
                "image_digest_matches": runtime_receipt[
                    "image_digest_matches"
                ],
                "cli_version_matches": True,
                "cleanup_verified": runtime_receipt["cleanup_verified"],
                "policy_id": runtime_receipt["isolation_policy_id"],
                "policy_sha256": runtime_receipt[
                    "isolation_policy_sha256"
                ],
                "model_started": False,
            },
            "service_identity_id": self.core_authority.service_identity_id,
            "base_url": self.core_authority.base_url,
            "environment_fallback_used": False,
            "project_configuration_valid": True,
        }
        if workspace_binding is not None:
            result["candidate_workspace_binding"] = workspace_binding
        return result

    def prepare_successor_recovery_destination(
        self,
        request: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Create an artifact-empty Core project without creating a Task."""

        journal_key = f"successor-recovery-destination:{idempotency_key}"
        prior = self.journal.read(journal_key)
        if prior is not None:
            return prior["result"]
        if (
            request.get("attempt_index") != 1
            or request.get("fresh_workspace") is not True
            or request.get("resume_in_place") is not False
            or request.get("core_project_id") is not None
        ):
            raise ValueError("recovery destination requires a fresh attempt-one project")
        task_id = str(request["task_id"])
        run_id = str(request["run_id"])
        workspace_root = self.run_root / "runs" / run_id
        if not workspace_root.exists():
            workspace = build_official_workspace(
                self.config.researchclawbench_root,
                task_id,
                self.run_root / "runs",
                run_id,
            )
        else:
            candidate_root = self.run_root / "sanitized_workspaces" / task_id
            workspace = WorkspaceReceipt(
                task_id=task_id,
                run_id=run_id,
                workspace=workspace_root,
                candidate_workspace=candidate_root,
                instructions=workspace_root / "INSTRUCTIONS.md",
                data=workspace_root / "data",
                related_work=workspace_root / "related_work",
                code=workspace_root / "code",
                outputs=workspace_root / "outputs",
                report=workspace_root / "report",
            )
            if not workspace.instructions.is_file() or not candidate_root.is_dir():
                raise ValueError("recovery destination workspace authority is incomplete")
        objective = workspace.instructions.read_text(encoding="utf-8")
        project_config = _project_config(self.config, task_id, objective)
        project_request = ProjectCreateV2(
            display_name=f"RCB recovery destination {request['experiment_id']}",
            config=project_config,
        ).model_dump(mode="json")
        archive_path = (
            self.run_root
            / "control"
            / run_id
            / "successor-recovery-destination-workspace.tar"
        )
        archive_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        archive_output = archive_path
        verify_only = archive_path.exists()
        if verify_only:
            archive_output = archive_path.with_name(
                f".{archive_path.name}.{secrets.token_hex(4)}.verify"
            )
        descriptor = os.open(
            archive_output,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            archive = write_workspace_archive(workspace.candidate_workspace, descriptor)
        finally:
            os.close(descriptor)
        if verify_only:
            if hashlib.sha256(archive_path.read_bytes()).hexdigest() != archive.content_sha256:
                archive_output.unlink(missing_ok=True)
                raise ValueError("recovery destination workspace archive changed")
            archive_output.unlink(missing_ok=True)
        with self._client() as client:
            project = client.json(
                "POST",
                "/v2/projects",
                payload=project_request,
                headers={"Idempotency-Key": f"{idempotency_key}:project"},
            )
            if project.get("active_project_head") is None:
                chunk_size = min(8 * 1024 * 1024, archive.byte_size)
                upload_request = WorkspaceUploadCreateV2(
                    expected_project_head_id=None,
                    expected_project_head_manifest_sha256=None,
                    expected_project_config_sha256=project["project_config_sha256"],
                    archive=archive,
                    chunk_byte_size=chunk_size,
                    chunk_count=(archive.byte_size + chunk_size - 1) // chunk_size,
                )
                upload = client.json(
                    "POST",
                    f"/v2/projects/{project['project_id']}/workspace-uploads",
                    payload=upload_request.model_dump(mode="json"),
                    headers={
                        "If-Match": project["etag"],
                        "Idempotency-Key": f"{idempotency_key}:upload",
                    },
                )
                with archive_path.open("rb") as stream:
                    for index in range(upload_request.chunk_count):
                        chunk = stream.read(upload_request.chunk_byte_size)
                        if index < int(upload.get("next_chunk_index", 0)):
                            continue
                        upload = client.json(
                            "PUT",
                            f"/v2/projects/{project['project_id']}/workspace-uploads/"
                            f"{upload['upload_id']}/chunks/{index}",
                            content=chunk,
                            headers={
                                "Content-Type": "application/octet-stream",
                                "X-OpenEvo-Chunk-SHA256": hashlib.sha256(chunk).hexdigest(),
                                "X-OpenEvo-Chunk-Byte-Size": str(len(chunk)),
                                "If-Match": upload["etag"],
                                "Idempotency-Key": f"{idempotency_key}:chunk:{index}",
                            },
                        )
                if upload.get("state") != "finalized":
                    client.json(
                        "POST",
                        f"/v2/projects/{project['project_id']}/workspace-uploads/"
                        f"{upload['upload_id']}/finalize",
                        payload={"expected_content_sha256": archive.content_sha256},
                        headers={
                            "If-Match": upload["etag"],
                            "Idempotency-Key": f"{idempotency_key}:finalize",
                        },
                    )
            project = client.json("GET", f"/v2/projects/{project['project_id']}")
            deadline = time.monotonic() + 30.0
            while project.get("state") != "ready" and time.monotonic() < deadline:
                time.sleep(0.25)
                project = client.json(
                    "GET", f"/v2/projects/{project['project_id']}"
                )
        head = project.get("active_project_head")
        if (
            project.get("state") != "ready"
            or not isinstance(head, dict)
            or head.get("generation") != 0
            or head.get("predecessor_project_head_id") is not None
            or head.get("evolution_revision", {}).get("artifact_count") != 0
        ):
            raise ValueError("recovery destination did not publish an empty genesis head")
        baseline = self.composites.register_baseline(
            project_id=project["project_id"],
            project_head=head,
        )
        result = {
            "project_id": project["project_id"],
            "project_head": head,
            "project_config_sha256": project["project_config_sha256"],
            "workspace_snapshot_id": head["workspace_snapshot"]["workspace_snapshot_id"],
            "workspace_manifest_sha256": head["workspace_snapshot"]["manifest_sha256"],
            "baseline_composite": baseline,
            "candidate_started": False,
            "task_created": False,
            "attempt_budget_consumed": False,
            "archive_sha256": archive.content_sha256,
        }
        self.journal.write(
            journal_key,
            {"request_sha256": canonical_sha256(request), "result": result},
        )
        return result

    def recover(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any] | None:
        completed = self.journal.read(idempotency_key + ":result")
        if completed is not None:
            if completed.get("request_sha256") != canonical_sha256(request):
                raise ValueError("candidate recovery request drifted")
            return completed["result"]
        journal = self.journal.read(idempotency_key + ":intent")
        if journal is None:
            return None
        if journal.get("request_sha256") != canonical_sha256(request):
            raise ValueError("candidate recovery request drifted")
        # Exact Core idempotency replays are safe; no second candidate Task can
        # be created under the same admission key.
        try:
            return self._drive(
                request,
                idempotency_key,
                journal=journal,
                recovery=True,
            )
        except CoreControlError as exc:
            raise CandidateAuthorityUnavailable(
                "candidate Core authority changed before sealing",
                reason_code=exc.reason_code,
            ) from exc

    def execute(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        completed = self.journal.read(idempotency_key + ":result")
        if completed is not None:
            return completed["result"]
        # Re-authenticate the exact generation before even the adapter-local
        # candidate intent is made durable.  A race after this point is still
        # caught by per-response generation fencing and the supervisor moves
        # to BLOCKED instead of remaining CANDIDATE_RUNNING.
        try:
            self.preflight(request)
        except CoreControlError as exc:
            raise CandidateAuthorityUnavailable(
                "candidate Core authority failed before intent persistence",
                reason_code=exc.reason_code,
            ) from exc
        journal = self.journal.read(idempotency_key + ":intent")
        if journal is None:
            journal = {"request_sha256": canonical_sha256(request), "stage": "intent"}
            self.journal.write(idempotency_key + ":intent", journal)
        try:
            return self._drive(
                request,
                idempotency_key,
                journal=journal,
                recovery=False,
            )
        except CoreControlError as exc:
            raise CandidateAuthorityUnavailable(
                "candidate Core authority changed before sealing",
                reason_code=exc.reason_code,
            ) from exc

    def _drive(
        self,
        request: dict[str, Any],
        key: str,
        *,
        journal: dict[str, Any],
        recovery: bool,
    ) -> dict[str, Any]:
        if request.get("operation_phase") not in {"workspace", "execute"} or request.get("fresh_workspace") is not True or request.get("resume_in_place") is not False:
            raise ValueError("candidate operation requires a fresh non-resumed attempt")
        task_id = str(request["task_id"])
        run_id = str(request["run_id"])
        workspace = self._candidate_workspace(request)
        if request["operation_phase"] == "workspace":
            return {
                "run_id": run_id,
                "candidate_output_root": os.fspath(workspace.workspace),
                "candidate_workspace_root": os.fspath(workspace.candidate_workspace),
                "workspace_spec_sha256": canonical_sha256(
                    {
                        "instructions": os.fspath(workspace.instructions),
                        "data": os.fspath(workspace.data),
                        "related_work": os.fspath(workspace.related_work),
                        "writable": ["code", "outputs", "report"],
                    }
                ),
                "fresh_workspace": True,
            }
        candidate_started = time.monotonic()
        with self._client() as client:
            overlay_id = request.get("task_local_overlay_id")
            overlay_sha256 = None
            task_local = None
            if overlay_id is not None:
                overlay_scope_id = request.get("task_local_overlay_scope_id")
                if not isinstance(overlay_scope_id, str) or not overlay_scope_id:
                    raise ValueError("task-local overlay Core scope is absent")
                attachment = client.json(
                    "GET",
                    f"/v2/internal/training-feedback/attachments/{overlay_id}",
                )
                task_local, overlay_sha256 = _closed_task_local_overlay(
                    attachment,
                    task_id=task_id,
                    core_task_scope_id=overlay_scope_id,
                )
            objective = _compose_candidate_objective(
                workspace.instructions,
                task_local_overlay=task_local,
            )
            project_config = _project_config(self.config, task_id, objective)
            project_request = ProjectCreateV2(
                display_name=f"RCB {request['experiment_id']}",
                config=project_config,
            ).model_dump(mode="json")
            expected_project = request.get("core_project_id")
            if expected_project is None:
                project = client.json("POST", "/v2/projects", payload=project_request, headers={"Idempotency-Key": f"{key}:project"})
            else:
                project = client.json("GET", f"/v2/projects/{expected_project}")
                if project.get("project_id") != expected_project:
                    raise ValueError("Core project identity changed between attempts")
            selected_composite_id = str(request["input_composite_id"])
            selected_before_upload = project.get("active_project_head")
            _require_selected_core_head(
                self.composites,
                selected_composite_id=selected_composite_id,
                active_project_head=selected_before_upload,
            )
            if project.get("active_project_head") is not None and project.get("config") != project_config.model_dump(mode="json"):
                head = project["active_project_head"]
                project = client.json(
                    "PATCH",
                    f"/v2/projects/{project['project_id']}",
                    payload={
                        "expected_project_head_id": head["project_head_id"],
                        "expected_project_head_manifest_sha256": head["manifest_sha256"],
                        "expected_project_config_sha256": project["project_config_sha256"],
                        "display_name": project["display_name"],
                        "config": project_config.model_dump(mode="json"),
                    },
                    headers={"If-Match": project["etag"], "Idempotency-Key": f"{key}:project-update"},
                )
                update_deadline = time.monotonic() + 120
                while project.get("state") == "transitioning" and time.monotonic() < update_deadline:
                    time.sleep(0.25)
                    project = client.json("GET", f"/v2/projects/{project['project_id']}")
                if project.get("state") not in {"ready", "not_ready"}:
                    raise CoreControlError("Core project configuration update did not settle")
            archive_path = self.run_root / "control" / run_id / "sanitized-workspace.tar"
            archive_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            archive_output = archive_path
            verify_only = archive_path.exists()
            if verify_only:
                archive_output = archive_path.with_name(
                    f".{archive_path.name}.{secrets.token_hex(4)}.verify"
                )
            descriptor = os.open(archive_output, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
            try:
                archive = write_workspace_archive(workspace.candidate_workspace, descriptor)
            finally:
                os.close(descriptor)
            if verify_only:
                if hashlib.sha256(archive_path.read_bytes()).hexdigest() != archive.content_sha256:
                    archive_output.unlink(missing_ok=True)
                    raise ValueError("candidate recovery workspace archive changed")
                archive_output.unlink(missing_ok=True)
            head = project.get("active_project_head")
            upload_request = WorkspaceUploadCreateV2(
                expected_project_head_id=None if head is None else head["project_head_id"],
                expected_project_head_manifest_sha256=None if head is None else head["manifest_sha256"],
                expected_project_config_sha256=project["project_config_sha256"],
                archive=archive,
                chunk_byte_size=min(8 * 1024 * 1024, archive.byte_size),
                chunk_count=(archive.byte_size + min(8 * 1024 * 1024, archive.byte_size) - 1) // min(8 * 1024 * 1024, archive.byte_size),
            )
            if head is not None:
                preseeded_authority = request.get(
                    "preseeded_workspace_authority"
                )
                if preseeded_authority is None:
                    successor_authority = request.get(
                        "successor_workspace_authority"
                    )
                    if successor_authority is None:
                        core_archive = archive
                        workspace_projection = None
                    else:
                        (
                            core_archive,
                            workspace_projection,
                        ) = _require_successor_core_workspace_projection(
                            authority=successor_authority,
                            sanitized_archive=archive,
                        )
                    expected_snapshot = _require_existing_workspace_snapshot(
                        project=project,
                        project_head=head,
                        archive=core_archive,
                        allow_recovery_needs_attention=recovery,
                    )
                    workspace_source = (
                        "successor_workspace"
                        if successor_authority is not None
                        else "cross_task_genesis"
                    )
                    self.journal.write(
                        key + ":existing-workspace",
                        {
                            "request_sha256": canonical_sha256(
                                {
                                    "project_head_id": head["project_head_id"],
                                    "core_archive": core_archive.model_dump(
                                        mode="json"
                                    ),
                                    "sanitized_archive": archive.model_dump(
                                        mode="json"
                                    ),
                                    "workspace_projection": workspace_projection,
                                }
                            ),
                            "result": expected_snapshot,
                        },
                    )
                else:
                    core_archive = archive
                    workspace_projection = None
                    expected_snapshot = _require_preseeded_workspace_authority(
                        authority=preseeded_authority,
                        project=project,
                        project_head=head,
                        archive=archive,
                    )
                    workspace_source = "recovery_preseed"
                    self.journal.write(
                        key + ":preseeded-workspace",
                        {
                            "request_sha256": canonical_sha256(
                                preseeded_authority
                            ),
                            "result": expected_snapshot,
                        },
                    )
                binding = _candidate_workspace_binding_receipt(
                    task_id=task_id,
                    attempt_index=int(request["attempt_index"]),
                    workspace_source=workspace_source,
                    workspace_root=workspace.candidate_workspace,
                    project=project,
                    project_head=head,
                    archive=archive,
                    core_archive=core_archive,
                    workspace_projection=workspace_projection,
                    expected_snapshot=expected_snapshot,
                    task_local_overlay_id=request.get("task_local_overlay_id"),
                )
                if request.get("candidate_workspace_binding") != binding:
                    raise CoreControlError(
                        "CANDIDATE_WORKSPACE_BINDING_DRIFT",
                        reason_code="candidate_workspace_binding_drift",
                    )
                self.journal.write(
                    key + ":workspace-binding",
                    {
                        "request_sha256": binding["content_sha256"],
                        "result": binding,
                    },
                )
            else:
                upload = client.json(
                    "POST", f"/v2/projects/{project['project_id']}/workspace-uploads",
                    payload=upload_request.model_dump(mode="json"),
                    headers={"If-Match": project["etag"], "Idempotency-Key": f"{key}:upload"},
                )
                with archive_path.open("rb") as stream:
                    for index in range(upload_request.chunk_count):
                        chunk = stream.read(upload_request.chunk_byte_size)
                        upload = client.json(
                            "PUT", f"/v2/projects/{project['project_id']}/workspace-uploads/{upload['upload_id']}/chunks/{index}",
                            content=chunk,
                            headers={
                                "Content-Type": "application/octet-stream",
                                "X-OpenEvo-Chunk-SHA256": hashlib.sha256(chunk).hexdigest(),
                                "X-OpenEvo-Chunk-Byte-Size": str(len(chunk)),
                                "If-Match": upload["etag"],
                                "Idempotency-Key": f"{key}:chunk:{index}",
                            },
                        )
                client.json(
                    "POST", f"/v2/projects/{project['project_id']}/workspace-uploads/{upload['upload_id']}/finalize",
                    payload={"expected_content_sha256": archive.content_sha256},
                    headers={"If-Match": upload["etag"], "Idempotency-Key": f"{key}:finalize"},
                )
            project = client.json("GET", f"/v2/projects/{project['project_id']}")
            deadline = time.monotonic() + 120
            while project.get("state") != "ready" and time.monotonic() < deadline:
                time.sleep(0.25)
                project = client.json("GET", f"/v2/projects/{project['project_id']}")
            if project.get("state") != "ready" or project.get("active_project_head") is None:
                raise CoreControlError("Core project did not publish the sanitized workspace")
            head = project["active_project_head"]
            if selected_composite_id == "c000":
                self.composites.register_baseline(
                    project_id=project["project_id"],
                    project_head=head,
                )
            task = client.json(
                "POST", "/v2/tasks",
                payload={
                    "project_id": project["project_id"],
                    "expected_project_admission_etag": project["admission_etag"],
                    "expected_project_head_id": head["project_head_id"],
                    "expected_project_head_manifest_sha256": head["manifest_sha256"],
                    "expected_project_config_sha256": project["project_config_sha256"],
                },
                headers={"Idempotency-Key": f"{key}:task"},
            )
            attempt_id = task["attempts"][0]["attempt_id"]
            authority = None
            deadline = time.monotonic() + float(self.config.require("candidate.attempt_timeout_seconds")) + 300
            while time.monotonic() < deadline:
                lifecycle = client.json(
                    "GET",
                    f"/v2/internal/training-attempts/{task['task_id']}/{attempt_id}/execution-status",
                )
                if not require_nonterminal_candidate_lifecycle(
                    lifecycle,
                    task_id=str(task["task_id"]),
                    attempt_id=str(attempt_id),
                ):
                    time.sleep(1.0)
                    continue
                try:
                    authority = client.json("GET", f"/v2/internal/training-attempts/{task['task_id']}/{attempt_id}")
                    break
                except CoreControlError as exc:
                    # A captured authority is intentionally unreadable until
                    # the attempt has sealed.  Only those explicit lifecycle
                    # responses are pollable; auth, generation and transport
                    # failures must surface immediately.
                    if exc.status_code not in {404, 409}:
                        raise
                    time.sleep(1.0)
            if authority is None:
                raise TimeoutError("Core did not seal the candidate SessionResult")
            # The immutable attempt authority can become readable immediately
            # before the separately journaled ``dataset_sealed`` timeline
            # event is visible.  Treat that interval as a normal publication
            # race, while still failing closed on duplicate dataset events or
            # an overall timeout.  A one-shot timeline read loses a completed
            # paid candidate despite the Core having durable session evidence.
            dataset = _wait_for_completed_dataset(
                client,
                task_id=str(task["task_id"]),
                attempt_id=str(attempt_id),
                deadline=deadline,
                pre_dataset_retry_idempotency_key=(
                    f"{key}:pre-dataset-successor-retry"
                ),
            )
            task = client.json("GET", f"/v2/tasks/{task['task_id']}")
            admitted_head = task.get("admission", {}).get(
                "predecessor_project_head"
            )
            if not isinstance(admitted_head, dict):
                raise ValueError("sealed candidate lacks admitted project-head authority")
            runtime_injection = _candidate_runtime_injection_authority(
                session_result=authority["session_result"],
                project_head=admitted_head,
                selected_composite_id=selected_composite_id,
                selected_composite=(
                    None
                    if selected_composite_id == "c000"
                    else self.composites.get_composite(selected_composite_id)
                ),
            )
            result_payload, _result_headers = client.bytes(f"/v2/internal/training-attempts/{task['task_id']}/{attempt_id}/workspace-result")
        output_archive = WorkspaceArchiveDeclarationV2.model_validate(
            authority["session_result"]["workspace_result"]["output_archive"]
        )
        expected_archive = output_archive.content_sha256
        successor_workspace_root = (
            self.run_root / "successor_workspaces" / task_id / run_id
        )
        materialized_archive, workspace_projection = (
            _materialize_successor_workspace(
                payload=result_payload,
                destination=successor_workspace_root,
                expected_archive=output_archive.model_dump(mode="json"),
                runtime_injection_receipt=runtime_injection.get(
                    "runtime_injection_receipt"
                ),
            )
        )
        _safe_export_result(result_payload, workspace.workspace, expected_archive)
        meta_path = workspace.workspace / "_meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta.update({"status": "completed", "exit_code": 0, "native_session_id": dataset["session_id"], "core_task_id": task["task_id"]})
        atomic_write_json(meta_path, meta)
        transcript_path = workspace.workspace / "_agent_output.jsonl"
        transcript_lines = "".join(
            json.dumps(trace, sort_keys=True, allow_nan=False) + "\n"
            for trace in authority["session_result"]["trajectory"]["traces"]
        )
        if transcript_path.exists():
            if transcript_path.read_text(encoding="utf-8") != transcript_lines:
                raise ValueError("recovered transcript conflicts with sealed SessionResult")
        else:
            with transcript_path.open("x", encoding="utf-8") as stream:
                stream.write(transcript_lines)
        execution = authority["execution_receipt"]
        successor_transition_id = task.get("successor_transition", {}).get(
            "successor_transition_id"
        )
        if not isinstance(successor_transition_id, str) or not successor_transition_id:
            raise ValueError("sealed candidate lacks successor transition authority")
        successor_workspace_body = {
            "schema_version": (
                "openevo.researchclawbench.successor_workspace_authority.v2"
            ),
            "task_id": task_id,
            "source_attempt_index": int(request["attempt_index"]),
            "source_run_id": run_id,
            "project_id": project["project_id"],
            "input_project_head_id": head["project_head_id"],
            "successor_transition_id": successor_transition_id,
            "workspace_handoff_id": execution["workspace_handoff_id"],
            "session_id": dataset["session_id"],
            "dataset_id": dataset["completed_dataset_id"],
            "session_result_sha256": execution["session_result_sha256"],
            "workspace_root": os.fspath(successor_workspace_root.resolve(strict=True)),
            "output_archive": materialized_archive.model_dump(mode="json"),
            "core_output_archive": output_archive.model_dump(mode="json"),
            "workspace_projection": workspace_projection,
        }
        successor_workspace_authority = {
            **successor_workspace_body,
            "content_sha256": canonical_sha256(successor_workspace_body),
        }
        result = {
            "run_id": run_id,
            "core_project_id": project["project_id"],
            "core_task_id": task["task_id"],
            "core_attempt_id": attempt_id,
            "workspace_binding_id": execution["workspace_handoff_id"],
            "task_request_id": execution["rollout_payload_sha256"],
            "session_result_id": execution["session_result_sha256"],
            "session_id": dataset["session_id"],
            "dataset_id": dataset["completed_dataset_id"],
            "dataset_revision": dataset["completed_dataset_revision"],
            "successor_transition_id": successor_transition_id,
            "successor_workspace_authority": successor_workspace_authority,
            "transcript_receipt": {"sha256": execution["session_result_sha256"], "path": os.fspath(transcript_path), "trace_count": len(authority["session_result"]["trajectory"]["traces"])},
            "candidate_output_root": os.fspath(workspace.workspace),
            "completed": True,
            "runtime_seconds": time.monotonic() - candidate_started,
            "cost_total_usd": None,
            "candidate_exit_state": "COMPLETED",
            "input_composite_id": selected_composite_id,
            "input_project_head_id": head["project_head_id"],
            "task_local_overlay_id": overlay_id,
            "task_local_overlay_sha256": overlay_sha256,
            "runtime_injection": runtime_injection,
            "tool_events": len(authority["session_result"]["trajectory"]["traces"]),
            "owned_resource_ids": [execution["session_id"]],
            "owned_resources": [
                {
                    "resource_id": execution["session_id"],
                    "kind": "openevo_session",
                    "identity": {
                        "session_id": execution["session_id"],
                        "core_project_id": project["project_id"],
                        "core_task_id": task["task_id"],
                        "core_attempt_id": attempt_id,
                    },
                    "active": False,
                }
            ],
        }
        self.journal.write(key + ":result", {"request_sha256": canonical_sha256(request), "result": result})
        # A second stable entry is used because the original intent is immutable.
        return result


def _copy_reconciliation_inputs(source: Path, destination: Path) -> None:
    """Copy only the original public candidate inputs into a fresh run root."""

    if source.is_symlink() or not source.is_dir():
        raise ValueError("reconciliation source run root is unsafe")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ("INSTRUCTIONS.md", "_meta.json"):
        item = source / name
        if item.is_symlink() or not item.is_file():
            raise ValueError("reconciliation source input file is unavailable")
        target = destination / name
        payload = item.read_bytes()
        if target.exists():
            if target.is_symlink() or not target.is_file() or target.read_bytes() != payload:
                raise ValueError("reconciliation input copy conflicts")
        else:
            target.write_bytes(payload)
            os.chmod(target, 0o600)
    for name in ("data", "related_work"):
        source_root = source / name
        target_root = destination / name
        if source_root.is_symlink() or not source_root.is_dir():
            raise ValueError("reconciliation source input directory is unavailable")
        target_root.mkdir(mode=0o700, exist_ok=True)
        for item in sorted(source_root.rglob("*")):
            relative = item.relative_to(source_root)
            if item.is_symlink():
                raise ValueError("reconciliation source input contains a symlink")
            target = target_root / relative
            if item.is_dir():
                target.mkdir(mode=0o700, exist_ok=True)
                continue
            if not item.is_file():
                raise ValueError("reconciliation source input is not regular")
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if target.exists():
                if target.is_symlink() or not target.is_file():
                    raise ValueError("reconciliation input target is unsafe")
                if target.stat().st_size != item.stat().st_size:
                    raise ValueError("reconciliation input target size conflicts")
                if hashlib.sha256(target.read_bytes()).digest() != hashlib.sha256(
                    item.read_bytes()
                ).digest():
                    raise ValueError("reconciliation input target content conflicts")
            else:
                shutil.copyfile(item, target, follow_symlinks=False)
                os.chmod(target, 0o600)
    for name in ("code", "outputs", "report", "report/images"):
        (destination / name).mkdir(mode=0o700, parents=True, exist_ok=True)


def _reconciled_run_sha256(root: Path) -> str:
    digest = hashlib.sha256(b"openevo-reconciled-run-v1\0")
    count = 0
    for item in sorted(root.rglob("*"), key=lambda value: value.as_posix()):
        relative = item.relative_to(root)
        if relative.as_posix() == ".openevo-reconciliation.json":
            continue
        if item.is_symlink():
            raise ValueError("reconciled run contains a symlink")
        if not item.is_file():
            continue
        payload = item.read_bytes()
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(payload)).encode("ascii"))
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
        count += 1
    if count == 0:
        raise ValueError("reconciled run is empty")
    return digest.hexdigest()


class CoreV2CandidateReconciliationPort(ProductionOperationPort):
    """Read one sealed Core authority and materialize it without a model call."""

    def __init__(
        self,
        config: ExperimentConfig,
        run_root: Path,
        *,
        core_authority: ManagedCoreControlAuthority,
        composites: LocalCompositePort | None = None,
    ) -> None:
        self.config = config
        self.run_root = run_root
        self.core_authority = core_authority
        self.composites = composites
        self.journal = _AuthorityJournal(run_root / "core_reconciliation_authority")

    @contextmanager
    def _client(self) -> Iterator[CoreControlV2Client]:
        client = CoreControlV2Client(self.core_authority)
        try:
            yield client
        finally:
            client.close()

    def recover(
        self, request: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any] | None:
        completed = self.journal.read(idempotency_key + ":result")
        if completed is None:
            return None
        if completed.get("request_sha256") != canonical_sha256(request):
            raise ValueError("candidate reconciliation recovery request drifted")
        return completed["result"]

    def execute(
        self, request: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]:
        completed = self.recover(request, idempotency_key)
        if completed is not None:
            return completed
        required = {
            "source_namespace",
            "source_state_sha256",
            "source_task_id",
            "source_attempt_index",
            "source_run_id",
            "source_candidate_output_root",
            "source_core_project_id",
            "source_core_task_id",
            "source_core_attempt_id",
            "source_session_id",
            "source_dataset_id",
            "source_dataset_revision",
            "source_session_result_sha256",
            "source_adapter_identity",
            "source_protocol_identity",
            "source_core_identity",
            "expected_model",
            "expected_harness",
            "expected_capture_mode",
            "expected_codex_version",
            "migration_executor_adapter_identity",
            "migration_executor_core_identity",
            "migration_protocol_identity",
            "migration_core_generation",
            "migration_release_identity",
            "fresh_workspace",
            "candidate_reexecuted",
            "additional_candidate_model_calls",
        }
        if not required.issubset(request):
            raise ValueError("candidate reconciliation request is incomplete")
        if (
            request["fresh_workspace"] is not True
            or request["candidate_reexecuted"] is not False
            or request["additional_candidate_model_calls"] != 0
            or isinstance(request["source_attempt_index"], bool)
            or not isinstance(request["source_attempt_index"], int)
            or not 0 <= request["source_attempt_index"] <= 2
        ):
            raise ValueError("candidate reconciliation would violate model-call policy")
        source_composite_id = request.get("source_input_composite_id", "c000")
        if not isinstance(source_composite_id, str) or not source_composite_id:
            raise ValueError("candidate reconciliation input composite is invalid")
        if request["source_attempt_index"] > 0 and (
            source_composite_id == "c000"
            or not isinstance(request.get("source_input_project_head_id"), str)
            or not request["source_input_project_head_id"]
            or self.composites is None
        ):
            raise ValueError("non-genesis reconciliation lacks composite authority")
        if (
            request["migration_core_generation"] != self.core_authority.generation
            or request["migration_release_identity"]
            != self.core_authority.release_identity
        ):
            raise CoreControlError("CORE_CONTROL_GENERATION_MISMATCH")
        source_root = Path(request["source_candidate_output_root"]).resolve(strict=True)
        expected_source = (
            self.config.experiment_root
            / "supervisor"
            / str(request["source_namespace"])
            / "runs"
            / str(request["source_run_id"])
        ).resolve(strict=True)
        if source_root != expected_source:
            raise ValueError("reconciliation source run root is not authoritative")
        task_id = str(request["source_core_task_id"])
        attempt_id = str(request["source_core_attempt_id"])
        dataset_id = str(request["source_dataset_id"])
        with self._client() as client:
            client.readiness()
            attempt = client.json(
                "GET", f"/v2/internal/training-attempts/{task_id}/{attempt_id}"
            )
            task = client.json("GET", f"/v2/tasks/{task_id}")
            project = client.json(
                "GET", f"/v2/projects/{request['source_core_project_id']}"
            )
            dataset = client.json(
                "GET", f"/v2/internal/training-feedback/datasets/{dataset_id}"
            )
            timeline = client.json("GET", f"/v2/tasks/{task_id}/timeline?limit=100")
            sealed = [
                item
                for item in timeline.get("items", [])
                if item.get("event_type") == "dataset_sealed"
                and item.get("attempt_id") == attempt_id
            ]
            attempt_rows = [
                item
                for item in task.get("attempts", [])
                if item.get("attempt_id") == attempt_id
            ]
            successor = task.get("successor_transition")
            if not isinstance(successor, dict):
                raise TypeError("sealed candidate lacks successor transition authority")
            successor_authority = client.json(
                "GET",
                "/v2/internal/training-successors/"
                + str(successor["successor_transition_id"]),
            )
            successor_state = successor_authority.get("transition", {}).get("state")
            session_result = attempt.get("session_result", {})
            execution = attempt.get("execution_receipt", {})
            predecessor_head = task.get("admission", {}).get(
                "predecessor_project_head"
            )
            selected_composite = None
            if source_composite_id != "c000":
                assert self.composites is not None
                selected_composite = self.composites.get_composite(
                    source_composite_id
                )
            agent = session_result.get("metadata", {}).get("agent", {})
            isolation = (
                session_result.get("metadata", {})
                .get("openevo", {})
                .get("credential_isolation", {})
            )
            project_task = project.get("config", {}).get("task", {})
            checks = {
                "project": project.get("project_id")
                == request["source_core_project_id"],
                "public_task": project_task.get("title")
                == request["source_task_id"],
                "task": task.get("task_id") == task_id
                and task.get("authoritative_attempt_id") == attempt_id,
                "attempt": len(attempt_rows) == 1,
                "input_project_head": isinstance(predecessor_head, dict)
                and predecessor_head.get("project_head_id")
                == request.get(
                    "source_input_project_head_id",
                    predecessor_head.get("project_head_id"),
                )
                and (
                    source_composite_id == "c000"
                    or selected_composite.get("core_project_head_id")
                    == predecessor_head.get("project_head_id")
                ),
                "session": dataset.get("session_id")
                == request["source_session_id"]
                == execution.get("session_id")
                == session_result.get("session_id"),
                "session_status": session_result.get("status") == "COMPLETED"
                and execution.get("terminal_status") == "COMPLETED",
                "dataset": dataset.get("completed_dataset_id") == dataset_id
                and dataset.get("completed_dataset_revision")
                == request["source_dataset_revision"],
                "dataset_unique": len(sealed) == 1
                and sealed[0].get("dataset_id") == dataset_id,
                "session_result": execution.get("session_result_sha256")
                == request["source_session_result_sha256"],
                "model": execution.get("model_ref") == request["expected_model"]
                and agent.get("model_name") == request["expected_model"]
                and execution.get("harness_id") == request["expected_harness"]
                and execution.get("capture_mode")
                == request["expected_capture_mode"]
                and isolation.get("codex_version")
                == request["expected_codex_version"],
                "terminal_successor": task.get("state") == "failed"
                and successor_state == "failed"
                and not isinstance(successor_authority.get("commit"), dict)
                and successor_authority.get("artifacts") == [],
            }
            if not all(checks.values()):
                failed = sorted(key for key, value in checks.items() if not value)
                raise ValueError(
                    "candidate sealed authority reconciliation failed: "
                    + ",".join(failed)
                )
            runtime_injection = None
            if request["source_attempt_index"] > 0:
                runtime_injection = _candidate_runtime_injection_authority(
                    session_result=session_result,
                    project_head=predecessor_head,
                    selected_composite_id=source_composite_id,
                    selected_composite=selected_composite,
                )
            output_archive = session_result.get("workspace_result", {}).get(
                "output_archive", {}
            )
            result_payload, _ = client.bytes(
                f"/v2/internal/training-attempts/{task_id}/{attempt_id}/workspace-result"
            )
        expected_archive_sha = output_archive.get("content_sha256")
        if (
            not isinstance(expected_archive_sha, str)
            or hashlib.sha256(result_payload).hexdigest() != expected_archive_sha
        ):
            raise ValueError("reconciled workspace result archive hash changed")
        run_id = str(request["source_run_id"])
        destination = self.run_root / "runs" / run_id
        marker_payload = {
            "schema_version": 1,
            "seal_origin": "reconciliation",
            "source_namespace": request["source_namespace"],
            "source_state_sha256": request["source_state_sha256"],
            "source_session_id": request["source_session_id"],
            "source_dataset_id": dataset_id,
            "source_dataset_revision": request["source_dataset_revision"],
            "source_session_result_sha256": request[
                "source_session_result_sha256"
            ],
            "workspace_result_archive_sha256": expected_archive_sha,
            "candidate_reexecuted": False,
            "additional_candidate_model_calls": 0,
        }
        marker = destination / ".openevo-reconciliation.json"
        if destination.exists():
            if not marker.is_file() or json.loads(
                marker.read_text(encoding="utf-8")
            ) != marker_payload:
                raise ValueError("reconciled candidate destination conflicts")
        else:
            staging = self.run_root / "reconciliation_staging" / hashlib.sha256(
                idempotency_key.encode("utf-8")
            ).hexdigest()
            staging.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _copy_reconciliation_inputs(source_root, staging)
            _safe_export_result(result_payload, staging, expected_archive_sha)
            meta_path = staging / "_meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta.update(
                {
                    "status": "completed",
                    "exit_code": 0,
                    "native_session_id": request["source_session_id"],
                    "core_task_id": task_id,
                    "seal_origin": "reconciliation",
                }
            )
            atomic_write_json(meta_path, meta)
            transcript_path = staging / "_agent_output.jsonl"
            transcript_lines = "".join(
                json.dumps(trace, sort_keys=True, allow_nan=False) + "\n"
                for trace in session_result.get("trajectory", {}).get("traces", [])
            )
            if transcript_path.exists():
                if transcript_path.read_text(encoding="utf-8") != transcript_lines:
                    raise ValueError("reconciled transcript conflicts")
            else:
                transcript_path.write_text(transcript_lines, encoding="utf-8")
                os.chmod(transcript_path, 0o600)
            atomic_write_json(staging / ".openevo-reconciliation.json", marker_payload)
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.replace(staging, destination)
            directory = os.open(
                destination.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
            )
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        result = {
            "run_id": run_id,
            "core_project_id": request["source_core_project_id"],
            "core_task_id": task_id,
            "core_attempt_id": attempt_id,
            "workspace_binding_id": execution["workspace_handoff_id"],
            "task_request_id": execution["rollout_payload_sha256"],
            "session_result_id": execution["session_result_sha256"],
            "session_id": request["source_session_id"],
            "dataset_id": dataset_id,
            "dataset_revision": request["source_dataset_revision"],
            "successor_transition_id": successor["successor_transition_id"],
            "transcript_receipt": {
                "sha256": execution["session_result_sha256"],
                "path": os.fspath(destination / "_agent_output.jsonl"),
                "trace_count": len(
                    session_result.get("trajectory", {}).get("traces", [])
                ),
            },
            "candidate_output_root": os.fspath(destination),
            "candidate_output_tree_sha256": _reconciled_run_sha256(destination),
            "completed": True,
            "runtime_seconds": float(
                session_result.get("timing", {}).get("run_ms", 0.0)
            )
            / 1000.0,
            "cost_total_usd": None,
            "candidate_exit_state": "COMPLETED",
            "input_composite_id": source_composite_id,
            "input_project_head_id": task["admission"]["predecessor_project_head"][
                "project_head_id"
            ],
            "task_local_overlay_id": request.get("source_task_local_overlay_id"),
            "task_local_overlay_sha256": request.get(
                "source_task_local_overlay_sha256"
            ),
            "tool_events": len(
                session_result.get("trajectory", {}).get("traces", [])
            ),
            "seal_origin": "reconciliation",
            "source_namespace": request["source_namespace"],
            "source_state_sha256": request["source_state_sha256"],
            "source_adapter_identity": request["source_adapter_identity"],
            "source_protocol_identity": request["source_protocol_identity"],
            "source_core_identity": request["source_core_identity"],
            "migration_executor_adapter_identity": request[
                "migration_executor_adapter_identity"
            ],
            "migration_executor_core_identity": request[
                "migration_executor_core_identity"
            ],
            "migration_protocol_identity": request["migration_protocol_identity"],
            "migration_core_generation": request["migration_core_generation"],
            "migration_release_identity": request["migration_release_identity"],
            "candidate_model_call_reused": True,
            "candidate_reexecuted": False,
            "additional_candidate_model_calls": 0,
            "owned_resource_ids": [request["source_session_id"]],
            "owned_resources": [
                {
                    "resource_id": request["source_session_id"],
                    "kind": "openevo_session",
                    "identity": {
                        "session_id": request["source_session_id"],
                        "core_project_id": request["source_core_project_id"],
                        "core_task_id": task_id,
                        "core_attempt_id": attempt_id,
                        "origin": "reconciliation",
                    },
                    "active": False,
                }
            ],
        }
        if runtime_injection is not None:
            result["runtime_injection"] = runtime_injection
        self.journal.write(
            idempotency_key + ":result",
            {"request_sha256": canonical_sha256(request), "result": result},
        )
        return result


class LocalValidationPort(ProductionOperationPort):
    def recover(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any] | None:
        candidate = request["candidate"]
        path = Path(candidate["candidate_output_root"]) / "validator.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def execute(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        candidate = request["candidate"]
        root = Path(candidate["candidate_output_root"]).resolve(strict=True)
        result = validate_workspace(root).to_dict()
        # A quality failure still consumed a paid immutable Attempt.  Freeze
        # every candidate-owned output before publishing either a passing or
        # failing validator receipt.
        freeze_candidate_outputs(root)
        closed = {
            "artifact_valid": result["passed"],
            "artifact_root_sha256": result["artifact_root_sha256"],
            "completeness": result["validator_completeness"],
            "validator_errors": result["errors"],
            "validator_receipt_id": f"validator-{canonical_sha256(result)[:24]}",
            "outputs_frozen_read_only": True,
        }
        atomic_write_json(root / "validator.json", closed)
        return closed


class CoreFeedbackPort(ProductionOperationPort):
    def __init__(
        self,
        *,
        core_authority: ManagedCoreControlAuthority,
        evaluator: DurableCommunityEvaluatorPort | None = None,
    ) -> None:
        self.core_authority = core_authority
        self.evaluator = evaluator

    @contextmanager
    def _client(self):
        client = CoreControlV2Client(self.core_authority)
        try:
            yield client
        finally:
            client.close()

    @staticmethod
    def _core_idempotency_key(supervisor_key: str) -> str:
        """Project the audit key into Core's closed identifier alphabet."""

        if not isinstance(supervisor_key, str) or not supervisor_key:
            raise ValueError("feedback attachment idempotency key is empty")
        return "rcb-feedback-" + hashlib.sha256(supervisor_key.encode()).hexdigest()

    def recover(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any] | None:
        core_key = self._core_idempotency_key(idempotency_key)
        with self._client() as client:
            dataset_authority = self._completed_dataset_authority(client, request)
            attachment_id = training_feedback_attachment_id_for_idempotency_key(
                core_key
            )
            try:
                attachment = client.json(
                    "GET",
                    f"/v2/internal/training-feedback/attachments/{attachment_id}",
                )
            except CoreControlError as exc:
                if exc.status_code != 404:
                    raise
                return None
            result = self._resolve(
                client,
                request,
                attachment,
                dataset_authority=dataset_authority,
            )
            feedback_source = request.get("feedback_source", "community_evaluator")
            result.update(
                {
                    "feedback_class": (
                        "MIXED"
                        if feedback_source == "artifact_validator"
                        else request["evaluation"].get("feedback_class", "SOFT_JUDGE")
                    ),
                    "feedback_source": feedback_source,
                    "judge_calls": 0 if feedback_source == "artifact_validator" else 1,
                }
            )
            return result

    def execute(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        core_key = self._core_idempotency_key(idempotency_key)
        feedback_source = request.get("feedback_source", "community_evaluator")
        if feedback_source == "artifact_validator":
            candidate = request["candidate"]
            validation = request["validation"]
            global_feedback, task_local_feedback = build_validator_feedback_layers(
                candidate, validation
            )
            feedback_class = "MIXED"
        else:
            evaluation = request["evaluation"]
            if self.evaluator is None:
                raise CoreControlError(
                    "durable evaluator feedback authority is unavailable"
                )
            operation_key = evaluation.get("idempotency_key")
            feedback_sha256 = evaluation.get("private_feedback_sha256")
            if not isinstance(operation_key, str) or not isinstance(
                feedback_sha256, str
            ):
                raise CoreControlError(
                    "evaluation receipt lacks private feedback authority"
                )
            private_feedback = self.evaluator.read_feedback_for_attachment(
                idempotency_key=operation_key,
                expected_sha256=feedback_sha256,
            )
            feedback_class = private_feedback.get("feedback_class")
            global_feedback = private_feedback.get("global_feedback")
            task_local_feedback = private_feedback.get("task_local_feedback")
            if (
                feedback_class not in {"HARD_GT", "SOFT_JUDGE", "MIXED"}
                or not isinstance(global_feedback, dict)
                or not isinstance(task_local_feedback, dict)
            ):
                raise CoreControlError(
                    "private evaluator feedback partition is invalid"
                )
            # The evaluator keeps task/attempt IDs in its private authority so
            # the durable Judge receipt remains self-describing.  They are
            # routing metadata, not evolution feedback, and Core's closed
            # SOFT_JUDGE schema intentionally does not admit them.  Verify the
            # binding before projecting only the feedback layer into Core.
            routing_identity = {
                "task_id": request["task_id"],
                "attempt_id": evaluation.get("attempt_id"),
            }
            for key, expected in routing_identity.items():
                if key in global_feedback and (
                    not isinstance(expected, str)
                    or global_feedback[key] != expected
                ):
                    raise CoreControlError(
                        "private evaluator feedback routing identity differs"
                    )
            global_feedback = {
                key: value
                for key, value in global_feedback.items()
                if key not in routing_identity
            }
        if task_local_feedback:
            task_local_feedback = {
                **task_local_feedback,
                "benchmark_task_scope_id": request["task_id"],
            }
        with self._client() as client:
            dataset_authority = self._completed_dataset_authority(client, request)
            attachment = client.json(
                "POST", "/v2/internal/training-feedback/attachments",
                payload={
                    "idempotency_key": core_key,
                    "session_id": request["session_id"],
                    # Completed-task datasets are sealed against the rollout
                    # task identity from ``openevo.session_completed``.  The
                    # science task ID is a different Core control-plane
                    # identity and must never be substituted here.
                    "task_id": dataset_authority["task_id"],
                    # Core successor preparation binds an attachment to the
                    # science task that produced the sealed rollout.  The
                    # public benchmark scope remains task-local metadata used
                    # only for same-benchmark-attempt injection.
                    "task_scope_id": request["core_task_id"],
                    "completed_dataset_id": request["dataset_id"],
                    "completed_dataset_revision": request["dataset_revision"],
                    "producer": "trusted_evaluator",
                    "feedback_class": feedback_class,
                    "global_feedback": global_feedback,
                    "task_local_feedback": task_local_feedback,
                    "created_at": None,
                },
            )
            result = self._resolve(
                client,
                request,
                attachment,
                dataset_authority=dataset_authority,
            )
            result.update(
                {
                    "feedback_class": feedback_class,
                    "feedback_source": feedback_source,
                    "judge_calls": 0 if feedback_source == "artifact_validator" else 1,
                }
            )
            return result

    @staticmethod
    def _completed_dataset_authority(
        client: CoreControlV2Client,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        authority = client.json(
            "GET",
            "/v2/internal/training-feedback/datasets/"
            f"{quote(str(request['dataset_id']), safe='')}",
        )
        expected = {
            "completed_dataset_id": request["dataset_id"],
            "completed_dataset_revision": request["dataset_revision"],
            "session_id": request["session_id"],
        }
        if any(authority.get(key) != value for key, value in expected.items()):
            raise CoreControlError(
                "completed dataset authority does not match the sealed candidate"
            )
        if not isinstance(authority.get("task_id"), str) or not authority["task_id"]:
            raise CoreControlError("completed dataset authority lacks a task identity")
        return authority

    @staticmethod
    def _resolve(
        client: CoreControlV2Client,
        request: dict[str, Any],
        attachment: dict[str, Any],
        *,
        dataset_authority: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        authority = dataset_authority or CoreFeedbackPort._completed_dataset_authority(
            client, request
        )
        resolved = client.json(
            "POST", "/v2/internal/training-feedback/resolve",
            payload={
                "completed_dataset_id": request["dataset_id"],
                "completed_dataset_revision": request["dataset_revision"],
                "attachment_ids": [attachment["attachment_id"]],
                "task_id": authority["task_id"],
                "task_scope_id": request["core_task_id"],
            },
        )
        return {
            "attachment_id": attachment["attachment_id"],
            "attachment_sha256": attachment["content_sha256"],
            "resolved_view_sha256": resolved["resolved_view_sha256"],
            "resolved_dataset_artifact_id": resolved["dataset_artifact"]["artifact_id"],
            # The next-attempt driver needs a durable Core authority identifier,
            # not merely a content digest.  It re-reads and validates the sealed
            # attachment before composing the same-task prompt overlay.
            "task_local_overlay_id": (
                attachment["attachment_id"]
                if attachment.get("task_local_feedback")
                else None
            ),
            "task_local_overlay_sha256": resolved["dataset_view"].get(
                "task_local_overlay_sha256"
            ),
            "task_local_overlay_scope_id": attachment.get("task_scope_id"),
            "authority": "evaluator_only",
            "successor_transition_id": request.get("successor_transition_id"),
        }


class CoreSuccessorPort(ProductionOperationPort):
    def __init__(
        self,
        config: ExperimentConfig,
        *,
        core_authority: ManagedCoreControlAuthority,
    ) -> None:
        self.config = config
        self.core_authority = core_authority

    def preflight(self, request: dict[str, Any]) -> dict[str, Any]:
        """Revalidate the pinned no-model adoption receipt and Core generation."""

        del request
        mount = self.config.reflector_credential_mount_readiness()
        client = CoreControlV2Client(self.core_authority)
        try:
            core = client.readiness()
        finally:
            client.close()
        if (
            mount.get("required") is not True
            or mount.get("ready") is not True
            or core.get("generation_matches") is not True
            or core.get("secret_recorded") is not False
        ):
            raise CoreControlError("REFLECTOR_CREDENTIAL_MOUNT_NOT_READY")
        return {
            **mount,
            "core_generation": core["generation"],
            "core_release_identity": core["release_identity"],
        }

    def _authority(self, transition_id: str) -> dict[str, Any]:
        client = CoreControlV2Client(self.core_authority)
        try:
            return client.json("GET", f"/v2/internal/training-successors/{transition_id}")
        finally:
            client.close()

    def recover(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any] | None:
        transition_id = request["attachment"].get("successor_transition_id")
        if not transition_id:
            return None
        client = CoreControlV2Client(self.core_authority)
        try:
            authority = client.json("GET", f"/v2/internal/training-successors/{transition_id}")
            transition = authority["transition"]
            if transition["state"] == "committed":
                return self._result(authority, request, client)
            failure = transition.get("error")
            if (
                transition["state"] == "failed"
                and isinstance(failure, dict)
                and failure.get("retryable") is False
            ):
                raise SuccessorRecoveryRequired(
                    self._recovery_checkpoint(
                        authority,
                        request=request,
                        idempotency_key=idempotency_key,
                    )
                )
            return None
        finally:
            client.close()

    def execute(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        transition_id = request["attachment"].get("successor_transition_id")
        if not transition_id:
            raise ValueError("evolution request lacks Core successor transition")
        client = CoreControlV2Client(self.core_authority)
        try:
            authority = client.json("GET", f"/v2/internal/training-successors/{transition_id}")
            transition = authority["transition"]
            if transition["state"] == "failed":
                failure = transition.get("error")
                if not isinstance(failure, dict) or failure.get("retryable") is not True:
                    raise SuccessorRecoveryRequired(
                        self._recovery_checkpoint(
                            authority,
                            request=request,
                            idempotency_key=idempotency_key,
                        )
                    )
                predecessor = transition["transition"]["predecessor_project_head"]
                client.json(
                    "POST", f"/v2/transitions/{transition_id}/retry",
                    payload={"expected_project_head_id": predecessor["project_head_id"]},
                    headers={"Idempotency-Key": idempotency_key},
                )
            deadline = time.monotonic() + 3 * 900
            while time.monotonic() < deadline:
                authority = client.json("GET", f"/v2/internal/training-successors/{transition_id}")
                if authority["transition"]["state"] == "committed":
                    return self._result(authority, request, client)
                if authority["transition"]["state"] == "failed":
                    raise SuccessorRecoveryRequired(
                        self._recovery_checkpoint(
                            authority,
                            request=request,
                            idempotency_key=idempotency_key,
                        )
                    )
                if authority["transition"]["state"] in {"cancelled", "superseded"}:
                    raise CoreControlError("Core successor terminated without a commit")
                time.sleep(1.0)
            raise TimeoutError("Core successor evolution exceeded protocol timeout")
        finally:
            client.close()

    def reconcile_terminal_successor(
        self,
        request: dict[str, Any],
        idempotency_key: str,
        checkpoint: dict[str, Any],
        reconciliation_id: str,
    ) -> dict[str, Any]:
        """Invoke Core's no-model completed-method commit tail exactly once."""

        attachment = request.get("attachment")
        if (
            not isinstance(attachment, dict)
            or checkpoint.get("supervisor_idempotency_key") != idempotency_key
            or checkpoint.get("successor_transition_id")
            != attachment.get("successor_transition_id")
            or checkpoint.get("model_side_effect_state")
            != "REQUIRES_CORE_RECOVERY_RESOLUTION"
            or not isinstance(reconciliation_id, str)
            or not reconciliation_id.startswith("successor-reconcile-")
        ):
            raise CoreControlError(
                "completed-method successor reconciliation identity drifted"
            )
        transition_id = checkpoint["successor_transition_id"]
        client = CoreControlV2Client(self.core_authority)
        try:
            core = client.readiness()
            if (
                core.get("generation_matches") is not True
                or core.get("secret_recorded") is not False
            ):
                raise CoreControlError(
                    "completed-method successor reconciliation Core is not ready"
                )
            authority = client.json(
                "GET",
                f"/v2/internal/training-successors/{transition_id}",
            )
            transition = authority.get("transition")
            attempts = authority.get("attempts")
            if not isinstance(transition, dict) or not isinstance(attempts, list):
                raise CoreControlError(
                    "completed-method successor authority is incomplete"
                )
            if transition.get("state") == "committed":
                if (
                    not attempts
                    or attempts[-1].get("retry_request_id") != reconciliation_id
                    or attempts[-1].get("reconciliation_only") is not True
                ):
                    raise CoreControlError(
                        "committed successor lacks reconciliation authority"
                    )
                return self._result(authority, request, client)
            source_attempt_count = checkpoint.get("transition_attempt_count")
            if (
                not isinstance(source_attempt_count, int)
                or source_attempt_count < 1
                or len(attempts) < source_attempt_count
            ):
                raise CoreControlError(
                    "completed-method successor attempt inventory changed"
                )
            if len(attempts) == source_attempt_count:
                terminal_sha256 = canonical_sha256(
                    {
                        "transition": transition,
                        "attempts": attempts,
                        "commit": authority.get("commit"),
                        "artifacts": authority.get("artifacts"),
                    }
                )
                if (
                    transition.get("state") != "failed"
                    or authority.get("commit") is not None
                    or authority.get("artifacts") != []
                    or attempts[-1].get("transition_attempt_id")
                    != checkpoint.get("latest_transition_attempt_id")
                    or terminal_sha256
                    != checkpoint.get("terminal_authority_sha256")
                ):
                    raise CoreControlError(
                        "completed-method source terminal authority changed"
                    )
            elif (
                len(attempts) != source_attempt_count + 1
                or attempts[-1].get("retry_request_id") != reconciliation_id
                or attempts[-1].get("reconciliation_only") is not True
            ):
                raise CoreControlError(
                    "completed-method reconciliation attempt is ambiguous"
                )
            client.json(
                "POST",
                f"/v2/internal/training-successors/{transition_id}/"
                "completed-methods-reconcile",
                payload={
                    "schema_version": (
                        "openevo.completed_methods_successor_"
                        "reconciliation_request.v1"
                    ),
                    "expected_project_head_id": (
                        checkpoint["predecessor_project_head_id"]
                    ),
                    "expected_terminal_attempt_id": (
                        checkpoint["latest_transition_attempt_id"]
                    ),
                    "expected_terminal_authority_sha256": (
                        checkpoint["terminal_authority_sha256"]
                    ),
                    "idempotency_key": reconciliation_id,
                    "model_execution_allowed": False,
                },
            )
            deadline = time.monotonic() + 15 * 60
            while time.monotonic() < deadline:
                authority = client.json(
                    "GET",
                    f"/v2/internal/training-successors/{transition_id}",
                )
                observed = authority.get("transition", {})
                if observed.get("state") == "committed":
                    return self._result(authority, request, client)
                if observed.get("state") == "failed":
                    raise SuccessorRecoveryRequired(checkpoint)
                if observed.get("state") in {"cancelled", "superseded"}:
                    raise CoreControlError(
                        "completed-method successor reconciliation terminated"
                    )
                time.sleep(1.0)
            raise TimeoutError(
                "completed-method successor reconciliation exceeded timeout"
            )
        finally:
            client.close()

    def _recovery_checkpoint(
        self,
        authority: dict[str, Any],
        *,
        request: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Close only an uncommitted, artifact-free terminal Core authority."""

        transition = authority.get("transition")
        transition_ref = (
            transition.get("transition") if isinstance(transition, dict) else None
        )
        attempts = authority.get("attempts")
        artifacts = authority.get("artifacts")
        attachment = request.get("attachment")
        latest = attempts[-1] if isinstance(attempts, list) and attempts else None
        failure = latest.get("error") if isinstance(latest, dict) else None
        transition_failure = (
            transition.get("error") if isinstance(transition, dict) else None
        )
        commit_absent = "commit" in authority and authority.get("commit") is None
        artifact_free = isinstance(artifacts, list) and not artifacts
        if not commit_absent or not artifact_free:
            raise CoreControlError(
                "Core successor terminal authority conflicts with commit or artifact evidence"
            )
        if (
            not isinstance(transition, dict)
            or transition.get("state") != "failed"
            or not isinstance(transition_failure, dict)
            or transition_failure.get("retryable") is not False
            or not isinstance(transition_ref, dict)
            or not isinstance(attempts, list)
            or not attempts
            or not isinstance(latest, dict)
            or latest.get("state") != "failed"
            or not isinstance(failure, dict)
            or failure.get("retryable") is not False
            or not isinstance(attachment, dict)
        ):
            raise CoreControlError(
                "Core successor terminal authority is incomplete or retryable"
            )
        transition_id = transition_ref.get("successor_transition_id")
        latest_transition_id = latest.get("successor_transition_id")
        request_transition_id = attachment.get("successor_transition_id")
        predecessor = transition_ref.get("predecessor_project_head")
        if (
            not isinstance(transition_id, str)
            or transition_id != latest_transition_id
            or transition_id != request_transition_id
            or not isinstance(predecessor, dict)
            or not isinstance(predecessor.get("project_head_id"), str)
            or not isinstance(predecessor.get("manifest_sha256"), str)
            or _SHA256.fullmatch(predecessor["manifest_sha256"]) is None
            or not isinstance(attachment.get("attachment_id"), str)
            or not isinstance(attachment.get("resolved_view_sha256"), str)
            or _SHA256.fullmatch(attachment["resolved_view_sha256"]) is None
            or not isinstance(latest.get("transition_attempt_id"), str)
            or not isinstance(failure.get("code"), str)
        ):
            raise CoreControlError(
                "Core successor recovery identity is incomplete or drifted"
            )
        checkpoint = {
            "schema_version": (
                "openevo.researchclawbench.successor_recovery_checkpoint.v1"
            ),
            "successor_transition_id": transition_id,
            "transition_attempt_count": len(attempts),
            "latest_transition_attempt_id": latest.get("transition_attempt_id"),
            "terminal_error_code": failure.get("code"),
            "predecessor_project_head_id": predecessor["project_head_id"],
            "predecessor_project_head_sha256": predecessor["manifest_sha256"],
            "attachment_id": attachment["attachment_id"],
            "resolved_view_sha256": attachment["resolved_view_sha256"],
            "core_generation": self.core_authority.generation,
            "core_release_identity": self.core_authority.release_identity,
            "supervisor_idempotency_key": idempotency_key,
            "terminal_authority_sha256": canonical_sha256(
                {
                    "transition": transition,
                    "attempts": attempts,
                    "commit": None,
                    "artifacts": [],
                }
            ),
            "commit_absent": True,
            "successor_artifact_count": 0,
            "recovery_mode": "core_append_only_new_generation",
            "model_side_effect_state": "REQUIRES_CORE_RECOVERY_RESOLUTION",
            "source_mutation_allowed": False,
            "candidate_reexecution_allowed": False,
            "judge_reexecution_allowed": False,
            "reflector_reexecution_allowed": False,
        }
        checkpoint["content_sha256"] = canonical_sha256(checkpoint)
        return checkpoint

    @staticmethod
    def _result(authority: dict[str, Any], request: dict[str, Any], client: CoreControlV2Client) -> dict[str, Any]:
        commit = authority.get("commit")
        manifest = commit.get("manifest") if isinstance(commit, dict) else None
        artifacts = manifest.get("artifacts", []) if isinstance(manifest, dict) else []
        artifact_authority = authority.get("artifacts", [])
        authority_by_id = {
            item["artifact_id"]: item
            for item in artifact_authority
            if isinstance(item, dict) and isinstance(item.get("artifact_id"), str)
        }
        by_type = {item["artifact_type"]: item for item in artifacts}
        if set(by_type) != set(ARTIFACT_TYPES):
            raise ValueError("Core successor commit lacks the triple-artifact composition")
        jobs = []
        for artifact_type in ARTIFACT_TYPES:
            item = by_type[artifact_type]
            artifact = authority_by_id.get(item["artifact_id"])
            if artifact is None or artifact.get("artifact_type") != artifact_type:
                raise ValueError("Core registry artifact type changed after successor commit")
            if artifact.get("promoted") is not True or artifact.get("state") not in {"sealed", "active"}:
                raise ValueError("Core successor artifact lacks sealed promotion authority")
            parent_ids = artifact.get("input_artifact_ids", [])
            action = artifact.get("admission_action")
            decision_id = artifact.get("admission_decision_id")
            decision_sha256 = artifact.get("admission_decision_sha256")
            proposal_ids = artifact.get("proposal_artifact_ids")
            content_admission = ArtifactContentAdmissionReceipt.model_validate(
                artifact.get("content_admission")
            )
            job_id = artifact.get("job_id")
            if (
                action not in {"update", "keep", "reject"}
                or not isinstance(job_id, str)
                or not job_id
                or not isinstance(decision_id, str)
                or not decision_id
                or not isinstance(decision_sha256, str)
                or len(decision_sha256) != 64
                or not isinstance(proposal_ids, list)
                or tuple(proposal_ids)
                != content_admission.proposal_artifact_ids
                or (
                    action == "update"
                    and (
                        item.get("origin") != "produced"
                        or item["artifact_id"] not in proposal_ids
                        or content_admission.passed is not True
                    )
                )
                or (
                    action in {"keep", "reject"}
                    and (
                        item.get("origin") != "inherited"
                        or item["artifact_id"] in proposal_ids
                    )
                )
            ):
                raise ValueError("Core successor lacks native admission authority")
            successor_sha256 = (
                artifact.get("payload_manifest_sha256")
                or artifact["registry_record_sha256"]
            )
            jobs.append(
                {
                    "job_id": job_id,
                    "artifact_type": artifact_type,
                    "action": action,
                    "parent_registry_id": parent_ids[0] if len(parent_ids) == 1 else None,
                    "parent_registry_ids": parent_ids,
                    "successor_registry_id": item["artifact_id"],
                    "successor_sha256": successor_sha256,
                    "proposal_ids": proposal_ids,
                    "promotion_result": (
                        "promoted"
                        if action == "update"
                        else "kept"
                        if action == "keep"
                        else "rejected"
                    ),
                    "admission_result": (
                        "rejected" if action == "reject" else "accepted"
                    ),
                    "admission_decision_id": decision_id,
                    "admission_decision_sha256": decision_sha256,
                    "content_admission": content_admission.model_dump(mode="json"),
                    "content_admission_sha256": content_admission.content_sha256,
                    "keep_receipt_id": decision_id if action == "keep" else None,
                    "dataset_view_sha256": request["attachment"]["resolved_view_sha256"],
                }
            )
        return {
            "jobs": jobs,
            "successor_transition_id": authority["transition"]["transition"]["successor_transition_id"],
            "successor_receipt_id": commit["manifest_sha256"],
            "successor_project_head": {
                "project_head_id": manifest["successor_project_head_id"],
                "manifest_sha256": manifest["successor_manifest_sha256"],
            },
        }


class LocalCompositePort(ProductionOperationPort):
    def __init__(self, root: Path) -> None:
        self.journal = _AuthorityJournal(root / "composites")

    def recover(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any] | None:
        value = self.journal.read(idempotency_key)
        return None if value is None else value["result"]

    def get_composite(self, composite_id: str) -> dict[str, Any]:
        value = self.journal.read(f"composite:{composite_id}")
        if value is None:
            raise ValueError("final composite is not present in the admitted registry view")
        return value["result"]

    def register_baseline(
        self,
        *,
        project_id: str,
        project_head: dict[str, Any],
    ) -> dict[str, Any]:
        evolution = project_head.get("evolution_revision", {})
        if evolution.get("artifact_count") != 0:
            raise ValueError("c000 baseline Core head is not artifact-empty")
        result = {
            "composite_id": "c000",
            "parent_composite_id": None,
            "core_project_id": project_id,
            "core_project_head_id": project_head["project_head_id"],
            "core_project_head_manifest_sha256": project_head["manifest_sha256"],
            "registry_artifact_ids": [],
            "artifact_hashes": {},
            "registry_artifacts": {},
            "composite_sha256": canonical_sha256(
                {
                    "composite_id": "c000",
                    "project_id": project_id,
                    "project_head_id": project_head["project_head_id"],
                    "project_head_manifest_sha256": project_head["manifest_sha256"],
                }
            ),
            "admission_receipt_ids": [],
            "admission_receipts": {},
        }
        self.journal.write(
            "composite:c000",
            {"request_sha256": result["composite_sha256"], "result": result},
        )
        return result

    def register_restored(
        self,
        *,
        selected_composite_id: str,
        project_head: dict[str, Any],
        restore_receipt_id: str,
        idempotency_key: str,
        project_id: str | None = None,
        restore_mode: str = "historical_restore",
    ) -> dict[str, Any]:
        prior = self.journal.read(idempotency_key)
        if prior is not None:
            return prior["result"]
        selected = self.get_composite(selected_composite_id)
        manifest = {
            "selected_composite_id": selected_composite_id,
            "source_composite_sha256": selected["composite_sha256"],
            "core_project_head_id": project_head["project_head_id"],
            "core_project_head_manifest_sha256": project_head["manifest_sha256"],
            "registry_artifacts": selected["registry_artifacts"],
            "admission_receipts": selected.get("admission_receipts", {}),
            "evolution_decision_receipts": selected.get(
                "evolution_decision_receipts", {}
            ),
            "restore_receipt_id": restore_receipt_id,
            "task_local_overlay_id": None,
        }
        if project_id is not None:
            manifest["destination_core_project_id"] = project_id
            manifest["restore_mode"] = restore_mode
        digest = canonical_sha256(manifest)
        result = {
            "composite_id": f"composite-{digest[:24]}",
            "parent_composite_id": selected_composite_id,
            "core_project_id": (
                selected.get("core_project_id")
                if project_id is None
                else project_id
            ),
            "core_project_head_id": project_head["project_head_id"],
            "core_project_head_manifest_sha256": project_head["manifest_sha256"],
            "registry_artifact_ids": selected["registry_artifact_ids"],
            "artifact_hashes": selected["artifact_hashes"],
            "registry_artifacts": selected["registry_artifacts"],
            "composite_sha256": digest,
            "admission_receipt_ids": [
                *selected["admission_receipt_ids"],
                restore_receipt_id,
            ],
            "admission_receipts": selected.get("admission_receipts", {}),
            "evolution_decision_receipts": selected.get(
                "evolution_decision_receipts", {}
            ),
            "historical_restore_source_composite_id": selected_composite_id,
            "historical_restore_receipt_id": restore_receipt_id,
            "restore_mode": restore_mode,
            "task_local_overlay_id": None,
        }
        closed = {"request_sha256": canonical_sha256(manifest), "result": result}
        self.journal.write(idempotency_key, closed)
        self.journal.write(f"composite:{result['composite_id']}", closed)
        return result

    def execute(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        prior = self.recover(request, idempotency_key)
        if prior is not None:
            return prior
        parent = self.get_composite(request["parent_composite_id"])
        jobs = request["evolution"]["jobs"]
        if len(jobs) != 3 or {item["artifact_type"] for item in jobs} != set(ARTIFACT_TYPES):
            raise ValueError("composite requires three native artifact results")
        for job in jobs:
            if job["action"] == "update" and job["promotion_result"] != "promoted":
                raise ValueError("composite cannot reference an unpromoted update")
            if job["action"] not in {"update", "keep", "reject"}:
                raise ValueError("composite artifact action is invalid")
            content = ArtifactContentAdmissionReceipt.model_validate(
                job.get("content_admission")
            )
            if (
                not isinstance(job.get("admission_decision_id"), str)
                or not isinstance(job.get("admission_decision_sha256"), str)
                or job.get("content_admission_sha256") != content.content_sha256
                or tuple(job.get("proposal_ids", ()))
                != content.proposal_artifact_ids
                or (
                    job["action"] != "reject"
                    and content.passed is not True
                )
            ):
                raise ValueError("composite artifact lacks native admission receipt")
        decision_receipts = {
            item["artifact_type"]: {
                "job_id": item["job_id"],
                "action": item["action"],
                "proposal_ids": item["proposal_ids"],
                "admission_decision_id": item["admission_decision_id"],
                "admission_decision_sha256": item["admission_decision_sha256"],
                "content_admission": item["content_admission"],
                "content_admission_sha256": item["content_admission_sha256"],
                "promotion_result": item["promotion_result"],
                "admission_result": item["admission_result"],
            }
            for item in jobs
        }
        parent_admission_receipts = parent.get("admission_receipts", {})
        admission_receipts: dict[str, Any] = {}
        for item in jobs:
            artifact_type = item["artifact_type"]
            if item["action"] == "update":
                active_receipt = decision_receipts[artifact_type]
            else:
                active_receipt = parent_admission_receipts.get(artifact_type)
                if not isinstance(active_receipt, dict):
                    raise ValueError(
                        "inherited composite artifact lacks prior admission receipt"
                    )
            active_content = ArtifactContentAdmissionReceipt.model_validate(
                active_receipt.get("content_admission")
            )
            if (
                active_content.passed is not True
                or active_receipt.get("content_admission_sha256")
                != active_content.content_sha256
            ):
                raise ValueError("active composite artifact lacks passing admission")
            admission_receipts[artifact_type] = active_receipt
        manifest = {
            "parent_composite_id": request["parent_composite_id"],
            "core_project_id": parent.get("core_project_id"),
            "core_project_head_id": request["evolution"]["successor_project_head"][
                "project_head_id"
            ],
            "core_project_head_manifest_sha256": request["evolution"][
                "successor_project_head"
            ]["manifest_sha256"],
            "registry_artifacts": {item["artifact_type"]: {"registry_id": item["successor_registry_id"], "sha256": item["successor_sha256"], "action": item["action"]} for item in jobs},
            "admission_receipts": admission_receipts,
            "evolution_decision_receipts": decision_receipts,
            "task_local_overlay_id": request.get("task_local_overlay_id"),
        }
        digest = canonical_sha256(manifest)
        result = {
            "composite_id": f"composite-{digest[:24]}",
            "parent_composite_id": request["parent_composite_id"],
            "core_project_id": manifest["core_project_id"],
            "core_project_head_id": manifest["core_project_head_id"],
            "core_project_head_manifest_sha256": manifest[
                "core_project_head_manifest_sha256"
            ],
            "registry_artifact_ids": [item["successor_registry_id"] for item in jobs],
            "artifact_hashes": {item["artifact_type"]: item["successor_sha256"] for item in jobs},
            "registry_artifacts": manifest["registry_artifacts"],
            "composite_sha256": digest,
            "admission_receipt_ids": [
                admission_receipts[item["artifact_type"]]["admission_decision_id"]
                for item in jobs
            ],
            "admission_receipts": admission_receipts,
            "evolution_decision_receipts": decision_receipts,
            "task_local_overlay_id": manifest["task_local_overlay_id"],
        }
        self.journal.write(idempotency_key, {"request_sha256": canonical_sha256(request), "result": result})
        self.journal.write(
            f"composite:{result['composite_id']}",
            {"request_sha256": canonical_sha256(request), "result": result},
        )
        return result


class LocalTaskLocalPort(ProductionOperationPort):
    def recover(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any] | None:
        return None

    def execute(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        overlay = request.get("overlay_id")
        # Overlay content is evaluator-private and immutable.  Destruction
        # removes only the active reference; the private audit receipt remains.
        return {"destroyed": True, "overlay_id": overlay, "active_reference_removed": True}


def _cross_task_content_admission_authority(
    composite: dict[str, Any],
) -> dict[str, Any]:
    """Verify active artifacts were scanned against a sealed private basis."""

    if composite.get("composite_id") == "c000":
        if (
            composite.get("registry_artifacts") != {}
            or composite.get("admission_receipts") != {}
            or composite.get("registry_artifact_ids") != []
        ):
            raise ValueError("c000 sanitizer authority is not artifact-empty")
        return {
            "mode": "empty_generation_zero",
            "artifact_count": 0,
            "passed": True,
        }
    registry = composite.get("registry_artifacts")
    receipts = composite.get("admission_receipts")
    if (
        not isinstance(registry, dict)
        or set(registry) != set(ARTIFACT_TYPES)
        or not isinstance(receipts, dict)
        or set(receipts) != set(ARTIFACT_TYPES)
    ):
        raise ValueError("cross-task composite lacks exact admission inventory")
    verified: dict[str, Any] = {}
    for artifact_type in ARTIFACT_TYPES:
        receipt = receipts[artifact_type]
        if not isinstance(receipt, dict):
            raise ValueError("cross-task admission receipt is invalid")
        content = ArtifactContentAdmissionReceipt.model_validate(
            receipt.get("content_admission")
        )
        registry_id = registry[artifact_type].get("registry_id")
        if (
            receipt.get("action") != "update"
            or not isinstance(registry_id, str)
            or registry_id not in content.proposal_artifact_ids
            or receipt.get("content_admission_sha256")
            != content.content_sha256
            or not isinstance(receipt.get("admission_decision_id"), str)
            or not isinstance(receipt.get("admission_decision_sha256"), str)
            or content.passed is not True
            or content.finding_count != 0
            or content.finding_categories
            or not content.source_artifact_ids
            or content.source_payload_sha256 is None
            or content.scanned_file_count < 1
            or content.scanned_byte_count < 1
        ):
            raise ValueError("cross-task active artifact lacks passing content admission")
        verified[artifact_type] = {
            "registry_id": registry_id,
            "admission_decision_id": receipt["admission_decision_id"],
            "admission_decision_sha256": receipt["admission_decision_sha256"],
            "content_admission_sha256": content.content_sha256,
            "basis_sha256": content.basis_sha256,
            "source_artifact_ids": list(content.source_artifact_ids),
            "source_payload_sha256": content.source_payload_sha256,
            "scanned_file_count": content.scanned_file_count,
            "scanned_byte_count": content.scanned_byte_count,
            "passed": True,
        }
    return {
        "mode": "core_native_content_admission",
        "artifact_count": 3,
        "artifacts": verified,
        "passed": True,
        "content_sha256": canonical_sha256(verified),
    }


class LocalSanitizerPort(ProductionOperationPort):
    def __init__(
        self,
        composites: LocalCompositePort,
        *,
        config: ExperimentConfig,
        root: Path,
        core_authority: ManagedCoreControlAuthority,
    ) -> None:
        self.composites = composites
        self.config = config
        self.root = root
        self.core_authority = core_authority
        self.journal = _AuthorityJournal(root / "cross_task_sanitizer")

    @staticmethod
    def _restore_key(idempotency_key: str) -> str:
        return "restore-" + hashlib.sha256(idempotency_key.encode()).hexdigest()

    @staticmethod
    def _restore_id(
        project_id: str,
        restore_key: str,
        *,
        cross_project: bool = False,
    ) -> str:
        payload: dict[str, object] = {
            "project_id": project_id,
            "restore_request_id": restore_key,
        }
        if cross_project:
            payload["cross_project"] = True
        seed = hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return f"historical-restore-{seed}"

    @staticmethod
    def _cross_restore_key(restore_key: str, next_task_id: str) -> str:
        return "cross-" + hashlib.sha256(
            f"{restore_key}:{next_task_id}".encode()
        ).hexdigest()

    def _prepare_cross_task_destination(
        self,
        *,
        next_task_id: str,
        cross_restore_key: str,
    ) -> dict[str, Any]:
        """Create an unused project owning the next task's exact workspace."""

        run_seed = hashlib.sha256(cross_restore_key.encode("utf-8")).hexdigest()
        run_id = f"{next_task_id}_a0_cross_task_{run_seed[:12]}"
        workspace_root = self.root / "runs" / run_id
        if not workspace_root.exists():
            workspace = build_official_workspace(
                self.config.researchclawbench_root,
                next_task_id,
                self.root / "runs",
                run_id,
            )
        else:
            candidate_root = self.root / "sanitized_workspaces" / next_task_id
            workspace = WorkspaceReceipt(
                task_id=next_task_id,
                run_id=run_id,
                workspace=workspace_root,
                candidate_workspace=candidate_root,
                instructions=workspace_root / "INSTRUCTIONS.md",
                data=workspace_root / "data",
                related_work=workspace_root / "related_work",
                code=workspace_root / "code",
                outputs=workspace_root / "outputs",
                report=workspace_root / "report",
            )
            if not workspace.instructions.is_file() or not candidate_root.is_dir():
                raise ValueError("cross-task destination workspace is incomplete")
        objective = workspace.instructions.read_text(encoding="utf-8")
        project_config = _project_config(self.config, next_task_id, objective)
        archive_path = (
            self.root
            / "control"
            / run_id
            / "cross-task-destination-workspace.tar"
        )
        archive_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        archive_output = archive_path
        verify_only = archive_path.exists()
        if verify_only:
            archive_output = archive_path.with_name(
                f".{archive_path.name}.{secrets.token_hex(4)}.verify"
            )
        descriptor = os.open(
            archive_output,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            archive = write_workspace_archive(
                workspace.candidate_workspace, descriptor
            )
        finally:
            os.close(descriptor)
        if verify_only:
            if (
                hashlib.sha256(archive_path.read_bytes()).hexdigest()
                != archive.content_sha256
            ):
                archive_output.unlink(missing_ok=True)
                raise ValueError("cross-task destination workspace archive changed")
            archive_output.unlink(missing_ok=True)
        project_request = ProjectCreateV2(
            display_name=f"RCB cross-task {next_task_id}",
            config=project_config,
        ).model_dump(mode="json")
        client = CoreControlV2Client(self.core_authority)
        try:
            project = client.json(
                "POST",
                "/v2/projects",
                payload=project_request,
                headers={"Idempotency-Key": f"{cross_restore_key}:project"},
            )
            if project.get("active_project_head") is None:
                chunk_size = min(8 * 1024 * 1024, archive.byte_size)
                upload_request = WorkspaceUploadCreateV2(
                    expected_project_head_id=None,
                    expected_project_head_manifest_sha256=None,
                    expected_project_config_sha256=project[
                        "project_config_sha256"
                    ],
                    archive=archive,
                    chunk_byte_size=chunk_size,
                    chunk_count=(archive.byte_size + chunk_size - 1) // chunk_size,
                )
                upload = client.json(
                    "POST",
                    f"/v2/projects/{project['project_id']}/workspace-uploads",
                    payload=upload_request.model_dump(mode="json"),
                    headers={
                        "If-Match": project["etag"],
                        "Idempotency-Key": f"{cross_restore_key}:upload",
                    },
                )
                with archive_path.open("rb") as stream:
                    for index in range(upload_request.chunk_count):
                        chunk = stream.read(upload_request.chunk_byte_size)
                        if index < int(upload.get("next_chunk_index", 0)):
                            continue
                        upload = client.json(
                            "PUT",
                            f"/v2/projects/{project['project_id']}/workspace-uploads/"
                            f"{upload['upload_id']}/chunks/{index}",
                            content=chunk,
                            headers={
                                "Content-Type": "application/octet-stream",
                                "X-OpenEvo-Chunk-SHA256": hashlib.sha256(
                                    chunk
                                ).hexdigest(),
                                "X-OpenEvo-Chunk-Byte-Size": str(len(chunk)),
                                "If-Match": upload["etag"],
                                "Idempotency-Key": (
                                    f"{cross_restore_key}:chunk:{index}"
                                ),
                            },
                        )
                if upload.get("state") != "finalized":
                    client.json(
                        "POST",
                        f"/v2/projects/{project['project_id']}/workspace-uploads/"
                        f"{upload['upload_id']}/finalize",
                        payload={
                            "expected_content_sha256": archive.content_sha256
                        },
                        headers={
                            "If-Match": upload["etag"],
                            "Idempotency-Key": f"{cross_restore_key}:finalize",
                        },
                    )
            deadline = time.monotonic() + 30.0
            project = client.json(
                "GET", f"/v2/projects/{project['project_id']}"
            )
            while project.get("state") != "ready" and time.monotonic() < deadline:
                time.sleep(0.25)
                project = client.json(
                    "GET", f"/v2/projects/{project['project_id']}"
                )
        finally:
            client.close()
        head = project.get("active_project_head")
        if (
            project.get("state") != "ready"
            or project.get("config") != project_config.model_dump(mode="json")
            or not isinstance(head, dict)
            or head.get("generation") not in {0, 1}
            or head.get("workspace_snapshot")
            != _workspace_snapshot_for_archive(
                project_id=project["project_id"], archive=archive
            ).model_dump(mode="json")
        ):
            raise ValueError("cross-task destination project authority drifted")
        return {
            "project_id": project["project_id"],
            "project_config_sha256": project["project_config_sha256"],
            "project_head": head,
            "workspace_archive_sha256": archive.content_sha256,
            "workspace_run_id": run_id,
            "candidate_started": False,
            "task_created": False,
            "attempt_budget_consumed": False,
        }

    def _result(
        self,
        request: dict[str, Any],
        *,
        restored: dict[str, Any],
        restore_key: str,
        cross_restored: dict[str, Any] | None = None,
        destination: dict[str, Any] | None = None,
        cross_restore_key: str | None = None,
    ) -> dict[str, Any]:
        composite = self.composites.get_composite(request["composite_id"])
        content_admission = _cross_task_content_admission_authority(composite)
        source_rebound = self.composites.register_restored(
            selected_composite_id=request["composite_id"],
            project_head=restored["successor_project_head"],
            restore_receipt_id=restored["restore_request_id"],
            idempotency_key=f"restore-composite:{restore_key}",
        )
        rebound = source_rebound
        if cross_restored is not None:
            if destination is None or cross_restore_key is None:
                raise TypeError("cross-project restore lacks destination authority")
            rebound = self.composites.register_restored(
                selected_composite_id=source_rebound["composite_id"],
                project_head=cross_restored["successor_project_head"],
                restore_receipt_id=cross_restored["restore_request_id"],
                idempotency_key=f"cross-project-composite:{cross_restore_key}",
                project_id=destination["project_id"],
                restore_mode="cross_project_fork",
            )
        if rebound.get("task_local_overlay_id") is not None:
            raise ValueError("cross-task restore carried a task-local overlay")
        result = {
            "passed": True,
            "composite_id": rebound["composite_id"],
            "sanitization_sha256": canonical_sha256(
                {
                    "request": request,
                    "source_composite": composite,
                    "source_restored_composite": source_rebound,
                    "restored_composite": rebound,
                    "restore_receipt_id": restored["restore_request_id"],
                    "cross_restore_receipt_id": (
                        None
                        if cross_restored is None
                        else cross_restored["restore_request_id"]
                    ),
                }
            ),
            "content_admission": content_admission,
            "content_admission_receipts": rebound.get(
                "admission_receipts", {}
            ),
            "task_local_overlay_carried": False,
            "historical_restore_receipt_id": restored["restore_request_id"],
            "cross_project_restore_receipt_id": (
                None
                if cross_restored is None
                else cross_restored["restore_request_id"]
            ),
            "source_composite_id": request["composite_id"],
            "restored_project_head_id": rebound["core_project_head_id"],
            "core_project_id": rebound["core_project_id"],
            "next_task_id": request.get("next_task_id"),
            "cross_project_forked": cross_restored is not None,
            "destination_workspace_run_id": (
                None if destination is None else destination["workspace_run_id"]
            ),
            "candidate_started": False,
            "attempt_budget_consumed": False,
        }
        self.journal.write(
            restore_key,
            {"request_sha256": canonical_sha256(request), "result": result},
        )
        return result

    def recover(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any] | None:
        restore_key = self._restore_key(idempotency_key)
        local = self.journal.read(restore_key)
        if local is not None:
            if local["request_sha256"] != canonical_sha256(request):
                raise ValueError("cross-task sanitizer recovery request drifted")
            return local["result"]
        # Re-enter execute for both same-project and cross-project recovery.
        # Every Core mutation below has an exact deterministic idempotency key,
        # and Core's restore POST returns the original closed commit on an exact
        # replay.  A speculative GET is weaker: it cannot reconstruct a partial
        # cross-project destination and some deployed releases map a missing
        # restore to a generic 500 instead of the intended not-found response.
        return None

    def execute(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        # Content-level leakage was already fail-closed by native admission.
        # Re-resolve the admitted composite here so an adapter-generated or
        # task-local identity cannot cross the task boundary.
        composite = self.composites.get_composite(request["composite_id"])
        if composite.get("composite_id") != request["composite_id"]:
            raise ValueError("cross-task composite identity changed")
        if request["composite_id"] != "c000" and len(composite.get("registry_artifacts", {})) not in {0, 3}:
            raise ValueError("cross-task composite lacks three admitted artifacts")
        _cross_task_content_admission_authority(composite)
        candidate = request.get("final_candidate")
        project_id = request.get("core_project_id")
        if not isinstance(candidate, dict) or not isinstance(project_id, str):
            raise TypeError("cross-task sanitizer lacks final candidate Core authority")
        transition_id = candidate.get("successor_transition_id")
        if not isinstance(transition_id, str):
            raise TypeError("cross-task sanitizer lacks final successor transition")
        restore_key = self._restore_key(idempotency_key)
        client = CoreControlV2Client(self.core_authority)
        try:
            deadline = time.monotonic() + 120
            while True:
                authority = client.json(
                    "GET", f"/v2/internal/training-successors/{transition_id}"
                )
                transition = authority["transition"]
                state = transition["state"]
                if state == "failed":
                    predecessor = transition["transition"]["predecessor_project_head"]
                    abandon_operation = client.json(
                        "POST",
                        f"/v2/transitions/{transition_id}/abandon",
                        payload={
                            "expected_project_head_id": predecessor["project_head_id"]
                        },
                        headers={"Idempotency-Key": f"{restore_key}:abandon"},
                    )
                    # Core completes transition abandonment synchronously before
                    # returning this succeeded operation.  The abandoned task is
                    # then closed, so another training-successor GET is no longer
                    # a valid poll and deployed Core maps it to 409:closed.
                    if (
                        abandon_operation.get("kind") != "transition_abandon"
                        or abandon_operation.get("status") != "succeeded"
                    ):
                        raise ValueError(
                            "final attempt successor abandonment is not authoritative"
                        )
                    break
                elif state == "cancelled":
                    break
                elif state == "committed":
                    raise ValueError(
                        "final attempt unexpectedly committed an evolution successor"
                    )
                elif time.monotonic() >= deadline:
                    raise ValueError(
                        "final attempt successor did not reach a restorable terminal state"
                    )
                time.sleep(0.25)
            project = client.json("GET", f"/v2/projects/{project_id}")
            active = project.get("active_project_head")
            if not isinstance(active, dict):
                raise TypeError("historical restore lacks active Core head")
            restored = client.json(
                "POST",
                f"/v2/internal/projects/{project_id}/historical-restores",
                payload={
                    "expected_project_head_id": active["project_head_id"],
                    "expected_project_head_manifest_sha256": active["manifest_sha256"],
                    "source_project_head_id": composite["core_project_head_id"],
                    "source_project_head_manifest_sha256": composite[
                        "core_project_head_manifest_sha256"
                    ],
                    "idempotency_key": restore_key,
                },
            )
        finally:
            client.close()
        next_task_id = request.get("next_task_id")
        if next_task_id is None:
            return self._result(
                request, restored=restored, restore_key=restore_key
            )
        if not isinstance(next_task_id, str) or not next_task_id:
            raise TypeError("cross-task sanitizer next task identity is invalid")
        cross_restore_key = self._cross_restore_key(restore_key, next_task_id)
        destination = self._prepare_cross_task_destination(
            next_task_id=next_task_id,
            cross_restore_key=cross_restore_key,
        )
        destination_head = destination["project_head"]
        client = CoreControlV2Client(self.core_authority)
        try:
            if destination_head.get("generation") == 0:
                cross_restored = client.json(
                    "POST",
                    f"/v2/internal/projects/{destination['project_id']}/"
                    "historical-restores",
                    payload={
                        "expected_project_head_id": destination_head[
                            "project_head_id"
                        ],
                        "expected_project_head_manifest_sha256": (
                            destination_head["manifest_sha256"]
                        ),
                        "source_project_head_id": restored[
                            "successor_project_head"
                        ]["project_head_id"],
                        "source_project_head_manifest_sha256": restored[
                            "successor_project_head"
                        ]["manifest_sha256"],
                        "idempotency_key": cross_restore_key,
                        "mode": "cross_project_fork",
                        "source_project_id": project_id,
                    },
                )
            else:
                cross_restore_id = self._restore_id(
                    destination["project_id"],
                    cross_restore_key,
                    cross_project=True,
                )
                cross_restored = client.json(
                    "GET",
                    f"/v2/internal/historical-restores/{cross_restore_id}",
                )
                if (
                    cross_restored.get("successor_project_head")
                    != destination_head
                ):
                    raise ValueError(
                        "cross-task destination head lacks its restore receipt"
                    )
        finally:
            client.close()
        return self._result(
            request,
            restored=restored,
            restore_key=restore_key,
            cross_restored=cross_restored,
            destination=destination,
            cross_restore_key=cross_restore_key,
        )


class CoreProjectFreezePort(ProductionOperationPort):
    """Seal the Community project in Core before any official execution."""

    def __init__(
        self,
        composites: LocalCompositePort,
        *,
        config: ExperimentConfig,
        core_authority: ManagedCoreControlAuthority,
    ) -> None:
        self.composites = composites
        self.config = config
        self.core_authority = core_authority

    def _payload(
        self,
        request: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        selected = request["selected_composite_id"]
        composite = self.composites.get_composite(selected)
        artifacts = composite["registry_artifacts"]
        if set(artifacts) != set(ARTIFACT_TYPES):
            raise ValueError("final freeze lacks triple-artifact authority")
        project_id = composite.get("core_project_id")
        if not isinstance(project_id, str):
            raise ValueError("final freeze lacks the Core project authority")
        required_identities = (
            "protocol_sha256",
            "core_identity_sha256",
            "adapter_identity_sha256",
        )
        if any(
            not isinstance(request.get(key), str)
            or len(request[key]) != 64
            for key in required_identities
        ):
            raise ValueError("final freeze lacks source identity fences")
        training_manifest = {
            "task_ids": request["task_ids"],
            "expected_candidate_attempts": request["expected_candidate_attempts"],
            "expected_reflector_cycles": request["expected_reflector_cycles"],
            "expected_evolution_jobs": request["expected_evolution_jobs"],
            "task_best": request["task_best"],
        }
        return {
            "schema_version": "openevo.project_freeze_request.v1",
            "project_id": project_id,
            "expected_project_head_id": composite["core_project_head_id"],
            "expected_project_head_manifest_sha256": composite[
                "core_project_head_manifest_sha256"
            ],
            "composite_id": selected,
            "composite_sha256": composite["composite_sha256"],
            "artifacts": [
                {
                    "artifact_type": artifact_type,
                    "registry_id": artifacts[artifact_type]["registry_id"],
                    "sha256": artifacts[artifact_type]["sha256"],
                    "frozen": True,
                }
                for artifact_type in sorted(ARTIFACT_TYPES)
            ],
            "protocol_sha256": request["protocol_sha256"],
            "core_identity_sha256": request["core_identity_sha256"],
            "adapter_identity_sha256": request["adapter_identity_sha256"],
            "runtime_digest": str(self.config.require("candidate.runtime_image")).split(
                "@", 1
            )[1],
            "codex_cli_version": self.config.require("candidate.codex_cli_version"),
            "model": self.config.require("candidate.model"),
            "reasoning_effort": self.config.require("candidate.reasoning_level"),
            "training_manifest_sha256": canonical_sha256(training_manifest),
            "budget_policy_sha256": canonical_sha256(
                self.config.require("budgets")
            ),
            "tool_policy_sha256": canonical_sha256(
                {
                    "candidate_network": self.config.require("candidate.network"),
                    "candidate_tool_network": self.config.require(
                        "candidate.tool_network"
                    ),
                    "max_tool_steps": self.config.require(
                        "candidate.max_tool_steps"
                    ),
                    "token_limit": self.config.require("candidate.token_limit"),
                }
            ),
            "idempotency_key": idempotency_key,
        }

    @staticmethod
    def _result(authority: dict[str, Any]) -> dict[str, Any]:
        artifacts = {
            item["artifact_type"]: {
                "registry_id": item["registry_id"],
                "sha256": item["sha256"],
                "frozen": True,
            }
            for item in authority["artifacts"]
        }
        if set(artifacts) != set(ARTIFACT_TYPES):
            raise ValueError("Core project freeze lost triple-artifact authority")
        return {
            **artifacts,
            "freeze_receipt_id": authority["freeze_receipt_id"],
            # ``authority_sha256`` is the canonical field on the immutable
            # Core freeze authority consumed by ``load_official_frozen_plan``.
            # Keep the explicit alias used by the official candidate request
            # surface so older adapter callers remain source compatible.
            "authority_sha256": authority["authority_sha256"],
            "freeze_authority_sha256": authority["authority_sha256"],
            "core_authoritative": authority["core_authoritative"],
            "frozen": authority["frozen"],
            "protocol_sha256": authority["protocol_sha256"],
            "core_identity_sha256": authority["core_identity_sha256"],
            "adapter_identity_sha256": authority["adapter_identity_sha256"],
            "core_project_id": authority["project_id"],
            "core_project_head_id": authority["project_head_id"],
            "core_project_head_manifest_sha256": authority[
                "project_head_manifest_sha256"
            ],
            "composite_id": authority["composite_id"],
            "composite_sha256": authority["composite_sha256"],
            "evolution_enabled": False,
            "reflector_enabled": False,
            "training_feedback_attachment_enabled": False,
            "hard_gt_teacher_enabled": False,
            "teacher_enabled": False,
            "task_local_overlay_enabled": False,
            "cross_task_state_updates": False,
            "official_feedback_released": False,
        }

    def recover(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any] | None:
        payload = self._payload(request, idempotency_key)
        client = CoreControlV2Client(self.core_authority)
        try:
            try:
                authority = client.json(
                    "GET",
                    f"/v2/internal/project-freezes/{payload['project_id']}",
                )
            except CoreControlError as exc:
                if exc.status_code == 404:
                    return None
                raise
        finally:
            client.close()
        expected = {
            "project_id": payload["project_id"],
            "project_head_id": payload["expected_project_head_id"],
            "project_head_manifest_sha256": payload[
                "expected_project_head_manifest_sha256"
            ],
            "composite_id": payload["composite_id"],
            "composite_sha256": payload["composite_sha256"],
            "protocol_sha256": payload["protocol_sha256"],
            "core_identity_sha256": payload["core_identity_sha256"],
            "adapter_identity_sha256": payload["adapter_identity_sha256"],
        }
        if any(authority.get(key) != value for key, value in expected.items()):
            raise ValueError("Core project freeze recovery identity drifted")
        return self._result(authority)

    def execute(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        client = CoreControlV2Client(self.core_authority)
        try:
            authority = client.json(
                "POST",
                "/v2/internal/project-freezes",
                payload=self._payload(request, idempotency_key),
            )
        finally:
            client.close()
        return self._result(authority)


def build_production_ports(
    config: ExperimentConfig,
    run_root: Path,
    *,
    core_authority: ManagedCoreControlAuthority,
) -> ProductionPorts:
    if not isinstance(core_authority, ManagedCoreControlAuthority):
        raise CoreControlError("MANAGED_CORE_CONTROL_AUTHORITY_REQUIRED")
    composite = LocalCompositePort(run_root)
    evaluator = DurableCommunityEvaluatorPort(
        build_production_community_evaluator(
            config=config,
            authority_root=run_root / "evaluator_private" / "durable_authority",
        )
    )
    return ProductionPorts(
        candidate=CoreV2CandidatePort(
            config,
            run_root,
            core_authority=core_authority,
            composites=composite,
        ),
        validation=LocalValidationPort(),
        evaluation=evaluator,
        attachment=CoreFeedbackPort(
            core_authority=core_authority,
            evaluator=evaluator,
        ),
        evolution=CoreSuccessorPort(config, core_authority=core_authority),
        composite=composite,
        task_local=LocalTaskLocalPort(),
        sanitizer=LocalSanitizerPort(
            composite,
            config=config,
            root=run_root,
            core_authority=core_authority,
        ),
        freeze=CoreProjectFreezePort(
            composite,
            config=config,
            core_authority=core_authority,
        ),
        reconciliation=CoreV2CandidateReconciliationPort(
            config,
            run_root,
            core_authority=core_authority,
            composites=composite,
        ),
    )


__all__ = [
    "CoreControlV2Client",
    "CoreV2CandidatePort",
    "CoreV2CandidateReconciliationPort",
    "build_production_ports",
    "build_validator_feedback_layers",
]
