"""Concrete managed-Core authority for one frozen official candidate.

The adapter creates only the sanitized workspace and drives authenticated Core
control APIs.  Core owns the frozen-project fork, ``TaskRequest`` admission,
``NativeTaskRunOwner``/``CodexHarness`` execution, and the immutable
``SessionResult``/completed-dataset authority.

Each instance is deliberately bound to one in-process
``ManagedCoreControlAuthority``.  The CLI creates and closes that attachment
for a single mutating supervisor transition; neither the bearer nor the
attachment is stored in the official supervisor database.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from openevo.backend.contracts.v2.models import (
    CapabilitiesResponseV2,
    ProjectCreateV2,
    ScienceProjectConfigV2,
    WorkspaceUploadCreateV2,
)
from openevo.backend.project_freeze_control import (
    FrozenProjectForkAuthorityV1,
    FrozenProjectForkRequestV1,
    ProjectFreezeAuthorityV1,
)
from openevo.rollout.models import SessionResult, SessionStatus
from openevo.workspace_archive import write_workspace_archive

from .config import ARTIFACT_TYPES, ExperimentConfig
from .managed_core_control import ManagedCoreControlAuthority
from .official_training_operations import OFFICIAL_DISABLED_POLICY
from .production_operation_ports import (
    CoreControlError,
    CoreControlV2Client,
    _AuthorityJournal,
    _candidate_runtime_injection_authority,
    require_managed_candidate_isolation_readiness,
    require_nonterminal_candidate_lifecycle,
    _safe_export_result,
    _wait_for_completed_dataset,
)
from .run_manifest import atomic_write_json
from .training_state_store import canonical_sha256
from .workspace import WorkspaceReceipt, build_official_workspace


def _official_project_config(
    config: ExperimentConfig,
    task_id: str,
    objective: str,
) -> ScienceProjectConfigV2:
    """Return a candidate-capable project with every evolution target closed."""

    targets = {
        artifact_type: {"enabled": False, "method": None, "config": {}}
        for artifact_type in (*ARTIFACT_TYPES, "parametric_memory")
    }
    return ScienceProjectConfigV2.model_validate(
        {
            "task": {"title": task_id, "objective": objective},
            "workspace": {
                "kind": "native_folder_snapshot",
                "display_name": f"{task_id} frozen official workspace",
            },
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


def _frozen_artifact_map(value: object) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict) or set(value) != set(ARTIFACT_TYPES):
        raise ValueError("official frozen artifact authority is incomplete")
    result: dict[str, dict[str, str]] = {}
    for artifact_type in ARTIFACT_TYPES:
        item = value[artifact_type]
        if not isinstance(item, dict):
            raise TypeError("official frozen artifact authority is invalid")
        registry_id = item.get("registry_id")
        digest = item.get("sha256")
        if (
            not isinstance(registry_id, str)
            or not registry_id
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("official frozen artifact authority is invalid")
        result[artifact_type] = {"registry_id": registry_id, "sha256": digest}
    return result


def _fork_request_id(request: FrozenProjectForkRequestV1) -> str:
    seed = hashlib.sha256(
        json.dumps(
            {
                "destination_project_id": request.destination_project_id,
                "idempotency_key": request.idempotency_key,
                "source_freeze_receipt_id": request.source_freeze_receipt_id,
                "source_project_id": request.source_project_id,
            },
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return f"frozen-project-fork-{seed[:32]}"


class ProductionFrozenOfficialCandidateCoreAuthority:
    """Execute/recover official candidates through one managed Core attachment."""

    def __init__(
        self,
        *,
        config: ExperimentConfig,
        run_root: str | Path,
        core_authority: ManagedCoreControlAuthority,
        official_task_ids: tuple[str, ...],
    ) -> None:
        if not isinstance(core_authority, ManagedCoreControlAuthority):
            raise TypeError("official candidate requires managed Core authority")
        self.config = config
        self.run_root = Path(os.path.abspath(run_root))
        if not self.run_root.is_relative_to(config.experiment_root):
            raise ValueError("official candidate run root escapes the experiment")
        self.run_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.core_authority = core_authority
        if (
            len(official_task_ids) != 40
            or len(set(official_task_ids)) != 40
            or any(not isinstance(task_id, str) for task_id in official_task_ids)
        ):
            raise ValueError("official candidate requires an exact 40-task allowlist")
        self.official_task_ids = official_task_ids
        self.journal = _AuthorityJournal(
            self.run_root / "official_core_candidate_authority"
        )

    @contextmanager
    def _client(self) -> Iterator[CoreControlV2Client]:
        client = CoreControlV2Client(self.core_authority)
        try:
            yield client
        finally:
            client.close()

    def _require_request(self, request: dict[str, Any]) -> dict[str, Any]:
        required = {
            "experiment_id",
            "task_id",
            "task_index",
            "attempt_index",
            "run_id",
            "fresh_workspace",
            "resume_in_place",
            "frozen_protocol_sha256",
            "freeze_receipt_sha256",
            "freeze_receipt_id",
            "freeze_authority_sha256",
            "frozen_composite_id",
            "frozen_composite_sha256",
            "frozen_registry_artifacts",
            "core_project_id",
            "core_project_head_id",
            "core_project_head_manifest_sha256",
            "official_policy",
        }
        if not isinstance(request, dict) or not required.issubset(request):
            raise ValueError("official candidate request is incomplete")
        if (
            request["attempt_index"] != 0
            or request["task_id"] not in self.official_task_ids
            or request["fresh_workspace"] is not True
            or request["resume_in_place"] is not False
            or request["official_policy"] != OFFICIAL_DISABLED_POLICY
            or any(
                key in request
                for key in (
                    "attachment_id",
                    "attachment_ids",
                    "teacher_payload",
                    "task_local_overlay_id",
                    "task_local_overlay_scope_id",
                    "evolution_job_id",
                    "evolution_job_ids",
                )
            )
        ):
            raise ValueError("official candidate request changed frozen policy")
        _frozen_artifact_map(request["frozen_registry_artifacts"])
        return json.loads(
            json.dumps(request, ensure_ascii=True, allow_nan=False, sort_keys=True)
        )

    def _source_authority(
        self,
        client: CoreControlV2Client,
        request: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        project_id = str(request["core_project_id"])
        project = client.json("GET", f"/v2/projects/{quote(project_id, safe='')}")
        freeze_payload = client.json(
            "GET", f"/v2/internal/project-freezes/{quote(project_id, safe='')}"
        )
        freeze = ProjectFreezeAuthorityV1.model_validate(freeze_payload)
        head = project.get("active_project_head")
        expected_artifacts = _frozen_artifact_map(request["frozen_registry_artifacts"])
        observed_artifacts = {
            item.artifact_type: {
                "registry_id": item.registry_id,
                "sha256": item.sha256,
            }
            for item in freeze.artifacts
        }
        if (
            project.get("project_id") != project_id
            or not isinstance(head, dict)
            or head.get("project_head_id") != request["core_project_head_id"]
            or head.get("manifest_sha256")
            != request["core_project_head_manifest_sha256"]
            or freeze.project_id != project_id
            or freeze.freeze_receipt_id != request["freeze_receipt_id"]
            or freeze.authority_sha256 != request["freeze_authority_sha256"]
            or freeze.project_head_id != request["core_project_head_id"]
            or freeze.project_head_manifest_sha256
            != request["core_project_head_manifest_sha256"]
            or freeze.composite_id != request["frozen_composite_id"]
            or freeze.composite_sha256 != request["frozen_composite_sha256"]
            or observed_artifacts != expected_artifacts
            or freeze.frozen is not True
            or freeze.evolution_enabled is not False
        ):
            raise CoreControlError("OFFICIAL_FROZEN_SOURCE_AUTHORITY_DRIFT")
        return project, freeze.model_dump(mode="json")

    def preflight_frozen_candidate(self, request: dict[str, Any]) -> dict[str, Any]:
        request = self._require_request(request)
        if not self.core_authority.managed_host_profile_ready:
            raise CoreControlError("MANAGED_CORE_HOST_PROFILE_UNAVAILABLE")
        with self._client() as client:
            readiness = client.readiness()
            capabilities = CapabilitiesResponseV2.model_validate(
                client.json(
                    "GET",
                    "/v2/capabilities?execution_mode=codex_subscription_transcript",
                )
            )
            candidate_runtime = client.json(
                "GET",
                "/v2/internal/managed-candidate/readiness",
            )
            self._source_authority(client, request)
        if capabilities.registry_digest != self.core_authority.registry_digest:
            raise CoreControlError("CORE_CAPABILITY_REGISTRY_MISMATCH")
        runtime_receipt = require_managed_candidate_isolation_readiness(
            candidate_runtime=candidate_runtime,
            core_readiness=readiness,
            expected_registry_digest=self.core_authority.registry_digest,
            failure_code="OFFICIAL_CANDIDATE_RUNTIME_NOT_READY",
        )
        config = _official_project_config(
            self.config,
            str(request["task_id"]),
            "Frozen official candidate capability preflight.",
        )
        if any(selection.enabled for selection in config.evolution.targets.values()):
            raise CoreControlError("OFFICIAL_EVOLUTION_TARGET_ENABLED")
        artifacts = _frozen_artifact_map(request["frozen_registry_artifacts"])
        return {
            **readiness,
            "core_generation": self.core_authority.generation,
            "release_identity": self.core_authority.release_identity,
            "source_project_id": request["core_project_id"],
            "source_freeze_receipt_id": request["freeze_receipt_id"],
            "source_freeze_authority_sha256": request[
                "freeze_authority_sha256"
            ],
            "source_project_head_id": request["core_project_head_id"],
            "source_project_head_manifest_sha256": request[
                "core_project_head_manifest_sha256"
            ],
            "context_artifact_ids": [
                artifacts[artifact_type]["registry_id"]
                for artifact_type in ARTIFACT_TYPES
            ],
            "source_project_head_verified": True,
            "triple_artifact_context_verified": True,
            "independent_project_creation_ready": True,
            "frozen_project_fork_supported": True,
            "all_evolution_targets_disabled": True,
            "managed_candidate_runtime": runtime_receipt,
            "managed_candidate_runtime_ready": True,
            "managed_candidate_credential_mount_adopted": True,
            "managed_candidate_codex_cli_version": "0.144.1",
            "managed_candidate_model_started": False,
            "environment_fallback_used": False,
            "model_started": False,
        }

    def recover_frozen_candidate(
        self,
        request: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        request = self._require_request(request)
        completed = self.journal.read(idempotency_key + ":result")
        if completed is not None:
            if completed.get("request_sha256") != canonical_sha256(request):
                raise ValueError("official candidate recovery request drifted")
            result = completed.get("result")
            if not isinstance(result, dict):
                raise ValueError("official candidate result journal is invalid")
            return result
        intent = self.journal.read(idempotency_key + ":intent")
        if intent is None:
            return None
        if intent.get("request_sha256") != canonical_sha256(request):
            raise ValueError("official candidate recovery request drifted")
        self.preflight_frozen_candidate(request)
        return self._drive(request, idempotency_key, recovery=True)

    def execute_frozen_candidate(
        self,
        request: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        request = self._require_request(request)
        completed = self.recover_frozen_candidate(request, idempotency_key)
        if completed is not None:
            return completed
        self.preflight_frozen_candidate(request)
        self.journal.write(
            idempotency_key + ":intent",
            {"request_sha256": canonical_sha256(request), "stage": "intent"},
        )
        return self._drive(request, idempotency_key, recovery=False)

    def _workspace(self, request: dict[str, Any]) -> WorkspaceReceipt:
        task_id = str(request["task_id"])
        run_id = str(request["run_id"])
        workspace_root = self.run_root / "runs" / run_id
        if not workspace_root.exists():
            return build_official_workspace(
                self.config.researchclawbench_root,
                task_id,
                self.run_root / "runs",
                run_id,
                allowed_task_ids=self.official_task_ids,
            )
        candidate_root = self.run_root / "sanitized_workspaces" / task_id
        receipt = WorkspaceReceipt(
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
        if not receipt.instructions.is_file() or not candidate_root.is_dir():
            raise ValueError("official candidate recovery workspace is incomplete")
        return receipt

    def _archive(self, workspace: WorkspaceReceipt, run_id: str) -> tuple[Path, Any]:
        archive_path = (
            self.run_root / "control" / run_id / "sanitized-workspace.tar"
        )
        archive_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        output_path = archive_path
        verify_only = archive_path.exists()
        if verify_only:
            output_path = archive_path.with_name(
                f".{archive_path.name}.{secrets.token_hex(4)}.verify"
            )
        descriptor = os.open(
            output_path,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
        )
        try:
            archive = write_workspace_archive(
                workspace.candidate_workspace,
                descriptor,
            )
        finally:
            os.close(descriptor)
        if verify_only:
            try:
                if (
                    hashlib.sha256(archive_path.read_bytes()).hexdigest()
                    != archive.content_sha256
                ):
                    raise ValueError("official candidate workspace archive changed")
            finally:
                output_path.unlink(missing_ok=True)
        return archive_path, archive

    def _find_submitted_task(
        self,
        client: CoreControlV2Client,
        *,
        destination_project_id: str,
        seeded_head_id: str,
    ) -> dict[str, Any]:
        page = client.json(
            "GET",
            "/v2/tasks?limit=100&project_id="
            + quote(destination_project_id, safe="")
            + "&direction=asc",
        )
        items = page.get("items")
        if not isinstance(items, list):
            raise CoreControlError("OFFICIAL_TASK_QUERY_INVALID")
        matches = [
            item
            for item in items
            if isinstance(item, dict)
            and item.get("project_id") == destination_project_id
            and item.get("admission", {})
            .get("predecessor_project_head", {})
            .get("project_head_id")
            == seeded_head_id
        ]
        if len(matches) != 1:
            raise CoreControlError(
                "OFFICIAL_TASK_SUBMISSION_OUTCOME_UNKNOWN"
                if not matches
                else "OFFICIAL_TASK_SUBMISSION_DUPLICATE"
            )
        return matches[0]

    def _drive(
        self,
        request: dict[str, Any],
        key: str,
        *,
        recovery: bool,
    ) -> dict[str, Any]:
        workspace = self._workspace(request)
        run_id = str(request["run_id"])
        task_id = str(request["task_id"])
        objective = workspace.instructions.read_text(encoding="utf-8")
        project_config = _official_project_config(
            self.config,
            task_id,
            objective,
        )
        project_request = ProjectCreateV2(
            display_name=f"RCB official {request['experiment_id']} {task_id}",
            config=project_config,
        ).model_dump(mode="json")
        candidate_started = time.monotonic()
        with self._client() as client:
            client.readiness()
            self._source_authority(client, request)
            project = client.json(
                "POST",
                "/v2/projects",
                payload=project_request,
                headers={"Idempotency-Key": f"{key}:project"},
            )
            if project.get("config") != project_config.model_dump(mode="json"):
                raise CoreControlError("OFFICIAL_DESTINATION_PROJECT_CONFIG_DRIFT")
            archive_path, archive = self._archive(workspace, run_id)
            genesis_key = key + ":destination-genesis"
            genesis_journal = self.journal.read(genesis_key)
            if genesis_journal is None:
                heads_page = client.json(
                    "GET",
                    f"/v2/projects/{quote(str(project['project_id']), safe='')}/heads"
                    "?limit=100&direction=asc",
                )
                heads = heads_page.get("items")
                if not isinstance(heads, list):
                    raise CoreControlError("OFFICIAL_DESTINATION_HEAD_QUERY_INVALID")
                generation_zero = [
                    item
                    for item in heads
                    if isinstance(item, dict) and item.get("generation") == 0
                ]
                if len(generation_zero) > 1:
                    raise CoreControlError("OFFICIAL_DESTINATION_GENESIS_DUPLICATE")
                if generation_zero:
                    genesis = generation_zero[0]
                else:
                    if project.get("active_project_head") is not None:
                        raise CoreControlError(
                            "OFFICIAL_DESTINATION_PROJECT_NOT_FRESH"
                        )
                    upload_request = WorkspaceUploadCreateV2(
                        expected_project_head_id=None,
                        expected_project_head_manifest_sha256=None,
                        expected_project_config_sha256=project[
                            "project_config_sha256"
                        ],
                        archive=archive,
                        chunk_byte_size=min(8 * 1024 * 1024, archive.byte_size),
                        chunk_count=(
                            archive.byte_size
                            + min(8 * 1024 * 1024, archive.byte_size)
                            - 1
                        )
                        // min(8 * 1024 * 1024, archive.byte_size),
                    )
                    upload = client.json(
                        "POST",
                        f"/v2/projects/{project['project_id']}/workspace-uploads",
                        payload=upload_request.model_dump(mode="json"),
                        headers={
                            "If-Match": project["etag"],
                            "Idempotency-Key": f"{key}:upload",
                        },
                    )
                    with archive_path.open("rb") as stream:
                        for index in range(upload_request.chunk_count):
                            chunk = stream.read(upload_request.chunk_byte_size)
                            upload = client.json(
                                "PUT",
                                f"/v2/projects/{project['project_id']}/"
                                f"workspace-uploads/{upload['upload_id']}/chunks/{index}",
                                content=chunk,
                                headers={
                                    "Content-Type": "application/octet-stream",
                                    "X-OpenEvo-Chunk-SHA256": hashlib.sha256(
                                        chunk
                                    ).hexdigest(),
                                    "X-OpenEvo-Chunk-Byte-Size": str(len(chunk)),
                                    "If-Match": upload["etag"],
                                    "Idempotency-Key": f"{key}:chunk:{index}",
                                },
                            )
                    upload = client.json(
                        "POST",
                        f"/v2/projects/{project['project_id']}/workspace-uploads/"
                        f"{upload['upload_id']}/finalize",
                        payload={"expected_content_sha256": archive.content_sha256},
                        headers={
                            "If-Match": upload["etag"],
                            "Idempotency-Key": f"{key}:finalize",
                        },
                    )
                    project = client.json(
                        "GET",
                        f"/v2/projects/{quote(str(project['project_id']), safe='')}",
                    )
                    deadline = time.monotonic() + 120.0
                    while (
                        project.get("state") != "ready"
                        and time.monotonic() < deadline
                    ):
                        time.sleep(0.25)
                        project = client.json(
                            "GET",
                            f"/v2/projects/{quote(str(project['project_id']), safe='')}",
                        )
                    genesis = project.get("active_project_head")
                    if (
                        project.get("state") != "ready"
                        or not isinstance(genesis, dict)
                        or genesis.get("generation") != 0
                    ):
                        raise CoreControlError(
                            "OFFICIAL_DESTINATION_WORKSPACE_NOT_READY"
                        )
                self.journal.write(
                    genesis_key,
                    {
                        "project_id": project["project_id"],
                        "project_config_sha256": project["project_config_sha256"],
                        "project_head_id": genesis["project_head_id"],
                        "project_head_manifest_sha256": genesis["manifest_sha256"],
                        "archive_sha256": archive.content_sha256,
                    },
                )
            else:
                if (
                    genesis_journal.get("project_id") != project["project_id"]
                    or genesis_journal.get("project_config_sha256")
                    != project["project_config_sha256"]
                    or genesis_journal.get("archive_sha256")
                    != archive.content_sha256
                ):
                    raise ValueError("official destination genesis recovery drifted")
                genesis = client.json(
                    "GET",
                    "/v2/project-heads/"
                    + quote(str(genesis_journal["project_head_id"]), safe=""),
                )
                if (
                    genesis.get("manifest_sha256")
                    != genesis_journal.get("project_head_manifest_sha256")
                    or genesis.get("generation") != 0
                ):
                    raise CoreControlError("OFFICIAL_DESTINATION_GENESIS_DRIFT")
            fork_request = FrozenProjectForkRequestV1(
                source_project_id=str(request["core_project_id"]),
                source_freeze_receipt_id=str(request["freeze_receipt_id"]),
                source_freeze_authority_sha256=str(
                    request["freeze_authority_sha256"]
                ),
                expected_source_project_head_id=str(
                    request["core_project_head_id"]
                ),
                expected_source_project_head_manifest_sha256=str(
                    request["core_project_head_manifest_sha256"]
                ),
                destination_project_id=str(project["project_id"]),
                expected_destination_project_head_id=str(
                    genesis["project_head_id"]
                ),
                expected_destination_project_head_manifest_sha256=str(
                    genesis["manifest_sha256"]
                ),
                expected_destination_project_config_sha256=str(
                    project["project_config_sha256"]
                ),
                idempotency_key=f"{key}:frozen-fork",
            )
            fork_key = key + ":fork-authority"
            fork_journal = self.journal.read(fork_key)
            if fork_journal is None:
                fork_receipt_id = _fork_request_id(fork_request)
                try:
                    fork_payload = client.json(
                        "GET",
                        "/v2/internal/frozen-project-forks/"
                        + quote(fork_receipt_id, safe=""),
                    )
                except CoreControlError as exc:
                    if exc.status_code != 404:
                        raise
                    fork_payload = client.json(
                        "POST",
                        "/v2/internal/frozen-project-forks",
                        payload=fork_request.model_dump(mode="json"),
                    )
                fork = FrozenProjectForkAuthorityV1.model_validate(fork_payload)
                self.journal.write(
                    fork_key,
                    {
                        "request_sha256": canonical_sha256(
                            fork_request.model_dump(mode="json")
                        ),
                        "fork_receipt_id": fork.fork_receipt_id,
                    },
                )
            else:
                if fork_journal.get("request_sha256") != canonical_sha256(
                    fork_request.model_dump(mode="json")
                ):
                    raise ValueError("official frozen fork recovery drifted")
                fork = FrozenProjectForkAuthorityV1.model_validate(
                    client.json(
                        "GET",
                        "/v2/internal/frozen-project-forks/"
                        + quote(str(fork_journal["fork_receipt_id"]), safe=""),
                    )
                )
            if fork.fork_receipt_id != _fork_request_id(fork_request):
                raise CoreControlError("OFFICIAL_FROZEN_FORK_IDENTITY_DRIFT")
            seeded = fork.seeded_destination_project_head.model_dump(mode="json")
            if (
                fork.destination_project_id != project["project_id"]
                or fork.source_project_id != request["core_project_id"]
                or fork.source_freeze_receipt_id != request["freeze_receipt_id"]
                or fork.source_freeze_authority_sha256
                != request["freeze_authority_sha256"]
                or fork.source_mutated is not False
                or fork.append_only is not True
                or fork.evolution_enabled is not False
            ):
                raise CoreControlError("OFFICIAL_FROZEN_FORK_AUTHORITY_DRIFT")
            project = client.json(
                "GET", f"/v2/projects/{quote(str(project['project_id']), safe='')}"
            )
            if (
                project.get("active_project_head", {}).get("project_head_id")
                != seeded["project_head_id"]
                or project.get("active_project_head", {}).get("manifest_sha256")
                != seeded["manifest_sha256"]
            ):
                raise CoreControlError("OFFICIAL_FROZEN_FORK_NOT_ACTIVE")
            task_intent_key = key + ":task-submission-intent"
            task_result_key = key + ":task-submission-result"
            task_intent = self.journal.read(task_intent_key)
            task_payload = {
                "project_id": project["project_id"],
                "expected_project_admission_etag": project["admission_etag"],
                "expected_project_head_id": seeded["project_head_id"],
                "expected_project_head_manifest_sha256": seeded[
                    "manifest_sha256"
                ],
                "expected_project_config_sha256": project[
                    "project_config_sha256"
                ],
            }
            task_request_sha256 = canonical_sha256(task_payload)
            if task_intent is None:
                if recovery:
                    raise CoreControlError("OFFICIAL_TASK_INTENT_MISSING_ON_RECOVERY")
                self.journal.write(
                    task_intent_key,
                    {
                        "request_sha256": task_request_sha256,
                        "destination_project_id": project["project_id"],
                        "seeded_head_id": seeded["project_head_id"],
                    },
                )
                task = client.json(
                    "POST",
                    "/v2/tasks",
                    payload=task_payload,
                    headers={"Idempotency-Key": f"{key}:task"},
                )
                self.journal.write(
                    task_result_key,
                    {
                        "request_sha256": task_request_sha256,
                        "task_id": task["task_id"],
                    },
                )
            else:
                if (
                    task_intent.get("request_sha256") != task_request_sha256
                    or task_intent.get("destination_project_id")
                    != project["project_id"]
                    or task_intent.get("seeded_head_id")
                    != seeded["project_head_id"]
                ):
                    raise ValueError("official task recovery intent drifted")
                task_result = self.journal.read(task_result_key)
                if task_result is None:
                    # The Task POST may have succeeded before its response was
                    # observed.  Recovery is deliberately GET-only from this
                    # point; replaying the mutating route could conceal a model
                    # call under a broken server-side idempotency boundary.
                    task = self._find_submitted_task(
                        client,
                        destination_project_id=str(project["project_id"]),
                        seeded_head_id=str(seeded["project_head_id"]),
                    )
                    self.journal.write(
                        task_result_key,
                        {
                            "request_sha256": task_request_sha256,
                            "task_id": task["task_id"],
                            "recovered_by_query": True,
                        },
                    )
                else:
                    if task_result.get("request_sha256") != task_request_sha256:
                        raise ValueError("official task recovery result drifted")
                    task = client.json(
                        "GET",
                        f"/v2/tasks/{quote(str(task_result['task_id']), safe='')}",
                    )
            attempts = task.get("attempts")
            if not isinstance(attempts, list) or len(attempts) != 1:
                raise CoreControlError("OFFICIAL_TASK_ATTEMPT_AUTHORITY_INVALID")
            core_task_id = str(task["task_id"])
            attempt_id = str(attempts[0]["attempt_id"])
            authority = None
            deadline = (
                time.monotonic()
                + float(self.config.require("candidate.attempt_timeout_seconds"))
                + 300.0
            )
            while time.monotonic() < deadline:
                lifecycle = client.json(
                    "GET",
                    "/v2/internal/training-attempts/"
                    f"{quote(core_task_id, safe='')}/{quote(attempt_id, safe='')}"
                    "/execution-status",
                )
                if not require_nonterminal_candidate_lifecycle(
                    lifecycle,
                    task_id=core_task_id,
                    attempt_id=attempt_id,
                ):
                    time.sleep(1.0)
                    continue
                try:
                    authority = client.json(
                        "GET",
                        "/v2/internal/training-attempts/"
                        f"{quote(core_task_id, safe='')}/{quote(attempt_id, safe='')}",
                    )
                    break
                except CoreControlError as exc:
                    if exc.status_code not in {404, 409}:
                        raise
                    time.sleep(1.0)
            if authority is None:
                raise TimeoutError("Core did not seal the official candidate")
            dataset = _wait_for_completed_dataset(
                client,
                task_id=core_task_id,
                attempt_id=attempt_id,
                deadline=deadline,
            )
            task = client.json("GET", f"/v2/tasks/{quote(core_task_id, safe='')}")
            admitted_head = task.get("admission", {}).get(
                "predecessor_project_head"
            )
            if (
                not isinstance(admitted_head, dict)
                or admitted_head.get("project_head_id") != seeded["project_head_id"]
                or task.get("successor_transition") is not None
            ):
                raise CoreControlError("OFFICIAL_TASK_CHANGED_FROZEN_HEAD")
            runtime_injection = _candidate_runtime_injection_authority(
                session_result=authority["session_result"],
                project_head=admitted_head,
                selected_composite_id=str(request["frozen_composite_id"]),
                selected_composite={
                    "registry_artifacts": _frozen_artifact_map(
                        request["frozen_registry_artifacts"]
                    )
                },
            )
            session = SessionResult.model_validate(authority["session_result"])
            result_payload = None
            if session.workspace_result is not None:
                result_payload, _headers = client.bytes(
                    "/v2/internal/training-attempts/"
                    f"{quote(core_task_id, safe='')}/{quote(attempt_id, safe='')}"
                    "/workspace-result"
                )
            # Recheck the frozen source after the candidate side effect.  The
            # fork contract promises append-only source preservation; this
            # read makes that promise part of the adapter receipt as well.
            self._source_authority(client, request)
        if session.workspace_result is not None:
            assert result_payload is not None
            _safe_export_result(
                result_payload,
                workspace.workspace,
                session.workspace_result.output_archive.content_sha256,
            )
        completed = session.status is SessionStatus.COMPLETED
        meta_path = workspace.workspace / "_meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta.update(
            {
                "status": "completed" if completed else "failed",
                "exit_code": 0 if completed else 1,
                "native_session_id": dataset["session_id"],
                "core_task_id": core_task_id,
            }
        )
        atomic_write_json(meta_path, meta)
        transcript_path = workspace.workspace / "_agent_output.jsonl"
        transcript_lines = "".join(
            json.dumps(trace, sort_keys=True, allow_nan=False) + "\n"
            for trace in authority["session_result"]["trajectory"]["traces"]
        )
        if transcript_path.exists():
            if transcript_path.read_text(encoding="utf-8") != transcript_lines:
                raise ValueError("official recovered transcript changed")
        else:
            with transcript_path.open("x", encoding="utf-8") as stream:
                stream.write(transcript_lines)
        execution = authority["execution_receipt"]
        artifacts = _frozen_artifact_map(request["frozen_registry_artifacts"])
        result = {
            "task_id": task_id,
            "run_id": run_id,
            "attempt_index": 0,
            "session_id": dataset["session_id"],
            "dataset_id": dataset["completed_dataset_id"],
            "dataset_revision": dataset["completed_dataset_revision"],
            "completed": completed,
            "sealed": True,
            "fresh_workspace": True,
            "candidate_output_root": os.fspath(workspace.workspace),
            "runtime_seconds": time.monotonic() - candidate_started,
            "cost_total_usd": None,
            "candidate_exit_state": session.status.value,
            "destination_core_project_id": project["project_id"],
            "core_task_id": core_task_id,
            "core_attempt_id": attempt_id,
            "workspace_binding_id": execution["workspace_handoff_id"],
            "task_request_id": execution["rollout_payload_sha256"],
            "session_result_id": execution["session_result_sha256"],
            "transcript_receipt": {
                "sha256": execution["session_result_sha256"],
                "path": os.fspath(transcript_path),
                "trace_count": len(session.trajectory.traces),
            },
            "runtime_injection": runtime_injection,
            "source_project_id": request["core_project_id"],
            "source_freeze_receipt_id": request["freeze_receipt_id"],
            "source_freeze_authority_sha256": request[
                "freeze_authority_sha256"
            ],
            "source_project_head_id": request["core_project_head_id"],
            "source_project_head_manifest_sha256": request[
                "core_project_head_manifest_sha256"
            ],
            "context_artifact_ids": [
                artifacts[artifact_type]["registry_id"]
                for artifact_type in ARTIFACT_TYPES
            ],
            "evolution_targets_enabled": [],
            "frozen_composite_id": request["frozen_composite_id"],
            "frozen_composite_sha256": request["frozen_composite_sha256"],
            "official_policy": dict(OFFICIAL_DISABLED_POLICY),
            "attachment_ids": [],
            "evolution_job_ids": [],
            "frozen_project_fork_receipt_id": fork.fork_receipt_id,
            "frozen_project_fork_authority_sha256": fork.authority_sha256,
            "seeded_destination_project_head_id": seeded["project_head_id"],
            "seeded_destination_project_head_manifest_sha256": seeded[
                "manifest_sha256"
            ],
            "frozen_project_fork_source_mutated": False,
            "frozen_project_fork_append_only": True,
            "owned_resource_ids": [execution["session_id"]],
            "owned_resources": [
                {
                    "resource_id": execution["session_id"],
                    "kind": "openevo_session",
                    "identity": {
                        "session_id": execution["session_id"],
                        "core_project_id": project["project_id"],
                        "core_task_id": core_task_id,
                        "core_attempt_id": attempt_id,
                    },
                    "active": False,
                }
            ],
        }
        self.journal.write(
            key + ":result",
            {"request_sha256": canonical_sha256(request), "result": result},
        )
        return result


__all__ = [
    "ProductionFrozenOfficialCandidateCoreAuthority",
    "_official_project_config",
]
