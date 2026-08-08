from __future__ import annotations

import hashlib
import json
import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Any, ClassVar

import openevo_researchclawbench.production_operation_ports as ports_module
import openevo_researchclawbench.production_training_operations as operations_module
import pytest
import yaml
from openevo_researchclawbench.config import (
    ARTIFACT_TYPES,
    CANARY_TASK,
    FROZEN_TASKS,
    ExperimentConfig,
)
from openevo_researchclawbench.evaluator_dependency_lock import (
    write_evaluator_dependency_lock,
)
from openevo_researchclawbench.managed_core_control import ManagedCoreControlAuthority
from openevo_researchclawbench.official_training_supervisor import (
    OfficialTrainingIdentity,
    load_official_frozen_plan,
)
from openevo_researchclawbench.production_operation_ports import (
    CoreControlError,
    CoreControlV2Client,
    CoreFeedbackPort,
    CoreProjectFreezePort,
    CoreSuccessorPort,
    CoreV2CandidatePort,
    LocalCompositePort,
    LocalSanitizerPort,
    PerItemEvolvedWorkspacePort,
    _candidate_runtime_injection_authority,
    _candidate_workspace_binding_receipt,
    _closed_core_control_error_code,
    _closed_task_local_overlay,
    _compose_candidate_objective,
    _cross_task_content_admission_authority,
    _materialize_successor_workspace,
    _project_config,
    _require_existing_workspace_snapshot,
    _require_preseeded_workspace_authority,
    _require_project_capabilities,
    _require_selected_core_head,
    _require_successor_core_workspace_projection,
    _require_successor_workspace_authority,
    _wait_for_completed_dataset,
    _workspace_snapshot_for_archive,
    build_production_ports,
    require_nonterminal_candidate_lifecycle,
)
from openevo_researchclawbench.production_training_operations import (
    FailureClass,
    JudgeCredentialsRequired,
    OperationStatus,
    ProductionPorts,
    ProductionTrainingOperations,
    SuccessorRecoveryRequired,
    judge_identity_preflight,
)
from openevo_researchclawbench.synthetic_training_fixture import (
    InjectedCrash,
    build_synthetic_ports,
)
from openevo_researchclawbench.training_state_store import TrainingStateStore
from openevo_researchclawbench.training_supervisor import (
    CandidateAuthorityUnavailable,
    CommunityTrainingSupervisor,
    SupervisorIdentity,
)
from openevo_researchclawbench.transition_engine import TrainingStage
from pydantic import SecretStr

from openevo.backend.contracts.v2.models import WorkspaceArchiveDeclarationV2
from openevo.evolution.framework.builtins import (
    ImplementationDistributionIdentity,
    build_builtin_registry,
)
from openevo.evolution.framework.capabilities import (
    CapabilityAudience,
    build_evolution_capabilities,
)
from openevo.evolution.framework.profiles import execution_profile_for_release_mode
from openevo.workspace_archive import write_workspace_archive

ROOT = Path(__file__).resolve().parents[4]
PROTOCOL = Path(
    os.environ.get(
        "OPENEVORESEARCHCLAWBENCH_TEST_PROTOCOL",
        ROOT / "experiments/sequential_task_reflector_evolution_v0/protocol/protocol.yaml",
    )
)


def _content_admission(
    proposal_id: str,
    *,
    source_id: str = "dataset-sealed",
) -> dict[str, Any]:
    payload = {
        "schema_version": "openevo.artifact_content_admission.v1",
        "basis_sha256": "a" * 64,
        "proposal_artifact_ids": [proposal_id],
        "source_artifact_ids": [source_id],
        "source_payload_sha256": "b" * 64,
        "scanned_file_count": 1,
        "scanned_byte_count": 64,
        "finding_count": 0,
        "finding_categories": [],
        "passed": True,
    }
    payload["content_sha256"] = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return payload


def _identity(config: ExperimentConfig) -> SupervisorIdentity:
    return SupervisorIdentity(
        protocol_sha256="1" * 64,
        core_identity_sha256="2" * 64,
        adapter_identity_sha256="3" * 64,
    )


def _core_authority() -> ManagedCoreControlAuthority:
    return ManagedCoreControlAuthority(
        base_url="http://127.0.0.1:17810",
        service_identity_id="core-service-synthetic",
        generation="1" * 32,
        release_identity="2" * 64,
        registry_digest="3" * 64,
        source_commit="4" * 40,
        status_proof="5" * 64,
        attached=True,
        managed_host_profile_ready=True,
        _bearer=SecretStr("z" * 64),
    )


def _project_head(*, generation: int, artifact_count: int) -> dict[str, Any]:
    project_id = "project-runtime"
    return {
        "schema_version": "2",
        "project_head_id": f"head-{generation}",
        "project_id": project_id,
        "generation": generation,
        "predecessor_project_head_id": (
            None if generation == 0 else f"head-{generation - 1}"
        ),
        "workspace_snapshot": {
            "schema_version": "2",
            "workspace_snapshot_id": f"workspace-{generation}",
            "project_id": project_id,
            "manifest_sha256": "1" * 64,
            "entry_count": 1,
            "byte_size": 1,
        },
        "evolution_revision": {
            "schema_version": "2",
            "evolution_revision_id": f"revision-{generation}",
            "project_id": project_id,
            "manifest_sha256": "2" * 64,
            "artifact_count": artifact_count,
        },
        "runtime_context_snapshot": {
            "schema_version": "2",
            "runtime_context_snapshot_id": f"runtime-{generation}",
            "project_id": project_id,
            "evolution_revision_id": f"revision-{generation}",
            "evolution_revision_manifest_sha256": "2" * 64,
            "registry_sha256": "3" * 64,
            "runtime_contract_sha256": "4" * 64,
            "manifest_sha256": "5" * 64,
        },
        "effective_execution_snapshot": {
            "schema_version": "2",
            "effective_execution_snapshot_id": f"execution-{generation}",
            "project_id": project_id,
            "execution_mode": "codex_subscription_transcript",
            "capture_mode": "transcript",
            "token_level_metrics_available": False,
            "producer_id": "producer-runtime",
            "snapshot_sha256": "6" * 64,
        },
        "registry_sha256": "3" * 64,
        "manifest_sha256": "7" * 64,
    }


def _session_result(
    head: dict[str, Any],
    evolution: dict[str, Any],
) -> dict[str, Any]:
    return {
        "session_id": "session-runtime",
        "task_id": "rollout-runtime",
        "status": "COMPLETED",
        "trajectory": {
            "status": "COMPLETED",
            "metadata": {},
            "traces": [],
        },
        "metadata": {
            "openevo": {
                "project_id": head["project_id"],
                "project_head_id": head["project_head_id"],
                "evolution_revision_id": head["evolution_revision"][
                    "evolution_revision_id"
                ],
                "runtime_context_snapshot_id": head[
                    "runtime_context_snapshot"
                ]["runtime_context_snapshot_id"],
            },
            "evolution": evolution,
        },
    }


def _runtime_injection_fixture() -> tuple[dict[str, Any], dict[str, Any]]:
    artifact_types = ("agent_system", "text_memory", "skill_bundle")
    files = [
        {
            "relative_path": "evolution/instruction.txt",
            "size_bytes": 8,
            "sha256": "8" * 64,
        },
        *[
            {
                "relative_path": f"evolution/{artifact_type}.txt",
                "size_bytes": index + 1,
                "sha256": str(index + 1) * 64,
            }
            for index, artifact_type in enumerate(artifact_types)
        ],
    ]
    files.sort(key=lambda item: item["relative_path"])
    files_by_path = {item["relative_path"]: item for item in files}
    artifacts = []
    composite = {"registry_artifacts": {}}
    for index, artifact_type in enumerate(artifact_types):
        artifact_id = f"registry-{artifact_type}"
        runtime_paths = [f"evolution/{artifact_type}.txt"]
        artifacts.append(
            {
                "artifact_id": artifact_id,
                "artifact_type": artifact_type,
                "content_sha256": str(index + 4) * 64,
                "runtime_paths": runtime_paths,
                "runtime_tree_sha256": ports_module.canonical_sha256(
                    {"files": [files_by_path[path] for path in runtime_paths]}
                ),
            }
        )
        composite["registry_artifacts"][artifact_type] = {
            "registry_id": artifact_id,
            "sha256": str(index + 7) * 64,
        }
    receipt = {
        "schema_version": "4",
        "context_id": "context-runtime",
        "context_manifest_sha256": "9" * 64,
        "revision_id": "revision-1",
        "runtime_context_snapshot_id": "runtime-1",
        "project_head_id": "head-1",
        "instruction_sha256": "8" * 64,
        "runtime_tree_sha256": ports_module.canonical_sha256({"files": files}),
        "files": files,
        "artifacts": artifacts,
    }
    return receipt, composite


def test_core_client_fences_generation_and_ignores_environment_bearer(monkeypatch) -> None:
    observed = {}

    class Response:
        status_code = 200
        content = b"{}"
        headers: ClassVar[dict[str, str]] = {
            "X-OpenEvo-Core-Generation": "1" * 32,
            "X-OpenEvo-Core-Release-Identity": "2" * 64,
        }

        def json(self):
            return {
                "preferred_major": 2,
                "provider_kind": "openevo_daemon",
                "status": "ready",
            }

    class Client:
        def __init__(self, **kwargs):
            observed.update(kwargs)

        def request(self, *_args, **_kwargs):
            return Response()

        def get(self, *_args, **_kwargs):
            return Response()

        def close(self):
            pass

    monkeypatch.setenv("OPENEVO_CORE_CONTROL_BEARER", "environment-must-not-win")
    monkeypatch.setattr(ports_module.httpx, "Client", Client)
    client = CoreControlV2Client(_core_authority())
    try:
        readiness = client.readiness()
    finally:
        client.close()
    assert readiness["generation_matches"] is True
    assert observed["trust_env"] is False
    assert observed["headers"]["Authorization"] == "Bearer " + "z" * 64
    assert "environment-must-not-win" not in observed["headers"]["Authorization"]


def test_core_client_rejects_generation_drift(monkeypatch) -> None:
    class Response:
        status_code = 200
        content = b"{}"
        headers: ClassVar[dict[str, str]] = {
            "X-OpenEvo-Core-Generation": "9" * 32,
            "X-OpenEvo-Core-Release-Identity": "2" * 64,
        }

        def json(self):
            return {}

    class Client:
        def __init__(self, **_kwargs):
            pass

        def request(self, *_args, **_kwargs):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(ports_module.httpx, "Client", Client)
    client = CoreControlV2Client(_core_authority())
    try:
        with pytest.raises(CoreControlError, match="GENERATION_MISMATCH"):
            client.json("GET", "/version")
    finally:
        client.close()


def test_core_client_retries_one_stale_get_connection_only(monkeypatch) -> None:
    calls: list[str] = []

    class Response:
        status_code = 200
        headers: ClassVar[dict[str, str]] = {
            "X-OpenEvo-Core-Generation": "1" * 32,
            "X-OpenEvo-Core-Release-Identity": "2" * 64,
        }

        def json(self):
            return {"status": "ready"}

    class Client:
        def __init__(self, **_kwargs):
            pass

        def request(self, method, *_args, **_kwargs):
            calls.append(method)
            if len(calls) == 1:
                raise ports_module.httpx.RemoteProtocolError(
                    "stale forwarded connection"
                )
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(ports_module.httpx, "Client", Client)
    client = CoreControlV2Client(_core_authority())
    try:
        assert client.json("GET", "/v2/system/status") == {"status": "ready"}
    finally:
        client.close()
    assert calls == ["GET", "GET"]

    calls.clear()
    client = CoreControlV2Client(_core_authority())
    try:
        with pytest.raises(
            ports_module.httpx.RemoteProtocolError,
            match="stale forwarded connection",
        ):
            client.json("POST", "/v2/projects", payload={})
    finally:
        client.close()
    assert calls == ["POST"]


@pytest.mark.parametrize(
    ("body", "expected_reason"),
    [
        (
            {
                "detail": (
                    "CANDIDATE_SUBSCRIPTION_ISOLATION_NOT_READY:"
                    "sandbox_namespace_unavailable"
                )
            },
            (
                "CANDIDATE_SUBSCRIPTION_ISOLATION_NOT_READY:"
                "sandbox_namespace_unavailable"
            ),
        ),
        ({"code": "MANAGED_CORE_NOT_READY"}, "MANAGED_CORE_NOT_READY"),
        ({"detail": "unsafe diagnostic / private/path"}, "closed"),
    ],
)
def test_core_client_projects_only_closed_http_error_codes(
    monkeypatch,
    body: dict[str, str],
    expected_reason: str,
) -> None:
    class Response:
        status_code = 503

        def json(self):
            return body

    class Client:
        def __init__(self, **_kwargs):
            pass

        def request(self, *_args, **_kwargs):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(ports_module.httpx, "Client", Client)
    client = CoreControlV2Client(_core_authority())
    try:
        with pytest.raises(CoreControlError) as captured:
            client.json("GET", "/v2/internal/managed-reflector/readiness")
    finally:
        client.close()
    assert captured.value.reason_code == expected_reason
    assert "unsafe diagnostic" not in str(captured.value)


def test_core_client_supports_a_validated_per_request_timeout(monkeypatch) -> None:
    observed: list[dict[str, object]] = []

    class Response:
        status_code = 200

        def __init__(self):
            self.headers = {
                "X-OpenEvo-Core-Generation": "1" * 32,
                "X-OpenEvo-Core-Release-Identity": "2" * 64,
            }

        def json(self):
            return {}

    class Client:
        def __init__(self, **_kwargs):
            pass

        def request(self, *_args, **kwargs):
            observed.append(kwargs)
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(ports_module.httpx, "Client", Client)
    client = CoreControlV2Client(_core_authority())
    try:
        client.json("POST", "/slow", payload={}, timeout_seconds=930.0)
        timeout = observed[-1]["timeout"]
        assert isinstance(timeout, ports_module.httpx.Timeout)
        assert timeout.connect == 10.0
        assert timeout.read == 930.0
        assert timeout.write == 930.0
        assert timeout.pool == 930.0
        client.json("GET", "/default")
        assert "timeout" not in observed[-1]
        for invalid in (0.0, -1.0, float("inf"), float("nan")):
            with pytest.raises(CoreControlError, match="TIMEOUT_INVALID"):
                client.json("GET", "/invalid", timeout_seconds=invalid)
    finally:
        client.close()


def _managed_candidate_readiness(
    *,
    service_generation: str,
    receipt_generation: str | None = None,
) -> dict[str, Any]:
    return {
        "core_generation": service_generation,
        "daemon_release_identity": "2" * 64,
        "release_registry_digest": "3" * 64,
        "candidate_runtime_ready": True,
        "credential_mount_adopted": True,
        "codex_cli_started": True,
        "model_started": False,
        "secret_recorded": False,
        "managed_candidate_runtime": {
            "generation_digest": receipt_generation or service_generation,
            "actual_cli_version": "0.144.1",
            "adoption_verified": True,
            "cleanup_verified": True,
            "path_fallback_allowed": False,
            "model_started": False,
            "codex_cli_auth_visible": True,
            "host_source_hidden": True,
            "tool_sandbox_credential_hidden": True,
            "tool_environment_clean": True,
            "parent_process_secret_unreadable": True,
            "generation_matches": True,
            "image_digest_matches": True,
            "isolation_probe_no_model": True,
            "isolation_policy_id": ports_module.CODEX_SUBSCRIPTION_POLICY_ID,
            "isolation_policy_sha256": (
                ports_module.CODEX_SUBSCRIPTION_POLICY_SHA256
            ),
            "authority_issued": True,
        },
    }


def test_candidate_preflight_accepts_distinct_typed_core_and_service_generations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    public_generation = "1" * 32
    service_generation = "a" * 64

    class Client:
        def readiness(self) -> dict[str, Any]:
            return {
                "service_reachable": True,
                "bearer_present": True,
                "bearer_valid": True,
                "generation_matches": True,
                "candidate_port_authorized": True,
                "secret_recorded": False,
                "generation": public_generation,
                "release_identity": "2" * 64,
            }

        def json(self, _method: str, path: str, **_kwargs: Any) -> dict[str, Any]:
            if path.startswith("/v2/capabilities"):
                return {}
            assert path == "/v2/internal/managed-candidate/readiness"
            return _managed_candidate_readiness(
                service_generation=service_generation,
            )

    config = ExperimentConfig.load(PROTOCOL)
    port = CoreV2CandidatePort(
        config,
        tmp_path,
        core_authority=_core_authority(),
        composites=LocalCompositePort(tmp_path),
    )

    @contextmanager
    def client_context():
        yield Client()

    monkeypatch.setattr(port, "_client", client_context)
    monkeypatch.setattr(
        ports_module,
        "_require_project_capabilities",
        lambda *_a, **_kw: None,
    )
    result = port.preflight(
        {
            "task_id": CANARY_TASK,
            "core_control_generation": public_generation,
            "core_control_release_identity": "2" * 64,
        }
    )
    assert result["generation"] == public_generation
    assert (
        result["managed_candidate_runtime"]["generation_digest"]
        == service_generation
    )
    assert public_generation != service_generation


def test_candidate_preflight_rejects_service_generation_receipt_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Client:
        def readiness(self) -> dict[str, Any]:
            return {
                "generation": "1" * 32,
                "release_identity": "2" * 64,
            }

        def json(self, _method: str, path: str, **_kwargs: Any) -> dict[str, Any]:
            if path.startswith("/v2/capabilities"):
                return {}
            assert path == "/v2/internal/managed-candidate/readiness"
            return _managed_candidate_readiness(
                service_generation="a" * 64,
                receipt_generation="b" * 64,
            )

    config = ExperimentConfig.load(PROTOCOL)
    port = CoreV2CandidatePort(
        config,
        tmp_path,
        core_authority=_core_authority(),
        composites=LocalCompositePort(tmp_path),
    )

    @contextmanager
    def client_context():
        yield Client()

    monkeypatch.setattr(port, "_client", client_context)
    monkeypatch.setattr(
        ports_module,
        "_require_project_capabilities",
        lambda *_a, **_kw: None,
    )
    with pytest.raises(CoreControlError, match="CANDIDATE_RUNTIME_NOT_READY"):
        port.preflight({"task_id": CANARY_TASK})


def test_candidate_port_authenticates_before_local_intent(tmp_path: Path, monkeypatch) -> None:
    config = ExperimentConfig.load(PROTOCOL)
    port = CoreV2CandidatePort(
        config,
        tmp_path,
        core_authority=_core_authority(),
        composites=LocalCompositePort(tmp_path),
    )
    monkeypatch.setattr(
        port,
        "preflight",
        lambda _request: (_ for _ in ()).throw(
            CoreControlError("synthetic bearer rejected")
        ),
    )
    with pytest.raises(CandidateAuthorityUnavailable, match="before intent persistence"):
        port.execute(
            {
                "task_id": CANARY_TASK,
                "run_id": "Life_005_a0_v2",
                "fresh_workspace": True,
                "resume_in_place": False,
            },
            "candidate-preflight-before-intent",
        )
    assert list((tmp_path / "core_candidate_authority").glob("*.json")) == []


def test_candidate_preseeded_workspace_authority_is_exact_and_fail_closed() -> None:
    archive = WorkspaceArchiveDeclarationV2(
        format="openevo_deterministic_tar_v1",
        media_type="application/vnd.openevo.workspace-tar",
        content_sha256=hashlib.sha256(b"\0" * 1024).hexdigest(),
        byte_size=1024,
        entry_count=0,
        extracted_byte_size=0,
    )
    project_id = "project-preseeded"
    snapshot = _workspace_snapshot_for_archive(
        project_id=project_id,
        archive=archive,
    ).model_dump(mode="json")
    head = {
        "project_head_id": "project-head-preseeded",
        "manifest_sha256": "1" * 64,
        "workspace_snapshot": snapshot,
    }
    authority = {
        "schema_version": (
            "openevo.researchclawbench.preseeded_workspace_authority.v1"
        ),
        "project_id": project_id,
        "project_head_id": head["project_head_id"],
        "project_head_manifest_sha256": head["manifest_sha256"],
        "workspace_snapshot_id": snapshot["workspace_snapshot_id"],
        "workspace_manifest_sha256": snapshot["manifest_sha256"],
        "archive_content_sha256": archive.content_sha256,
        "archive_byte_size": archive.byte_size,
        "archive_entry_count": archive.entry_count,
        "archive_extracted_byte_size": archive.extracted_byte_size,
        "seed_request_id": "recovery-seed-exact",
        "seed_sha256": "2" * 64,
    }
    assert _require_preseeded_workspace_authority(
        authority=authority,
        project={"project_id": project_id, "state": "ready"},
        project_head=head,
        archive=archive,
    ) == snapshot

    with pytest.raises(
        CoreControlError,
        match="CANDIDATE_PRESEEDED_WORKSPACE_AUTHORITY_DRIFT",
    ):
        _require_preseeded_workspace_authority(
            authority={**authority, "archive_content_sha256": "3" * 64},
            project={"project_id": project_id, "state": "ready"},
            project_head=head,
            archive=archive,
        )


def test_candidate_later_attempt_reuses_only_the_exact_existing_workspace() -> None:
    archive = WorkspaceArchiveDeclarationV2(
        format="openevo_deterministic_tar_v1",
        media_type="application/vnd.openevo.workspace-tar",
        content_sha256=hashlib.sha256(b"\0" * 1024).hexdigest(),
        byte_size=1024,
        entry_count=0,
        extracted_byte_size=0,
    )
    project = {"project_id": "project-normal-attempt", "state": "ready"}
    snapshot = _workspace_snapshot_for_archive(
        project_id=project["project_id"],
        archive=archive,
    ).model_dump(mode="json")
    head = {
        "project_head_id": "project-head-after-evolution",
        "workspace_snapshot": snapshot,
    }

    assert _require_existing_workspace_snapshot(
        project=project,
        project_head=head,
        archive=archive,
    ) == snapshot

    recovery_project = {**project, "state": "needs_attention"}
    with pytest.raises(
        CoreControlError,
        match="CANDIDATE_EXISTING_WORKSPACE_SNAPSHOT_DRIFT",
    ):
        _require_existing_workspace_snapshot(
            project=recovery_project,
            project_head=head,
            archive=archive,
        )
    assert _require_existing_workspace_snapshot(
        project=recovery_project,
        project_head=head,
        archive=archive,
        allow_recovery_needs_attention=True,
    ) == snapshot

    with pytest.raises(
        CoreControlError,
        match="CANDIDATE_EXISTING_WORKSPACE_SNAPSHOT_DRIFT",
    ):
        _require_existing_workspace_snapshot(
            project={**project, "state": "transitioning"},
            project_head=head,
            archive=archive,
            allow_recovery_needs_attention=True,
        )

    with pytest.raises(
        CoreControlError,
        match="CANDIDATE_EXISTING_WORKSPACE_SNAPSHOT_DRIFT",
    ):
        _require_existing_workspace_snapshot(
            project=project,
            project_head={
                **head,
                "workspace_snapshot": {
                    **snapshot,
                    "manifest_sha256": "f" * 64,
                },
            },
            archive=archive,
        )


def test_candidate_successor_workspace_is_materialized_and_attempt_aligned(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    for relative in (
        "data",
        "related_work",
        "code",
        "outputs",
        "report/images",
        "artifacts",
        "skills",
    ):
        (source / relative).mkdir(parents=True, exist_ok=True)
    (source / "INSTRUCTIONS.md").write_text("closed task\n", encoding="utf-8")
    (source / "data/public.txt").write_text("public\n", encoding="utf-8")
    (source / "code/result.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / "outputs/result.json").write_text("{}\n", encoding="utf-8")
    (source / "report/report.md").write_text("# Result\n", encoding="utf-8")
    archive_path = tmp_path / "source.tar"
    descriptor = os.open(
        archive_path,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
        0o600,
    )
    try:
        archive = write_workspace_archive(source, descriptor)
    finally:
        os.close(descriptor)

    run_root = tmp_path / "run"
    destination = (
        run_root
        / "successor_workspaces"
        / "Astronomy_004"
        / "Astronomy_004_a0_v13"
    )
    materialized, projection = _materialize_successor_workspace(
        payload=archive_path.read_bytes(),
        destination=destination,
        expected_archive=archive.model_dump(mode="json"),
    )
    assert materialized == archive
    assert projection["stripped_entry_count"] == 0

    authority_body = {
        "schema_version": (
            "openevo.researchclawbench.successor_workspace_authority.v2"
        ),
        "task_id": "Astronomy_004",
        "source_attempt_index": 0,
        "source_run_id": "Astronomy_004_a0_v13",
        "project_id": "project-successor",
        "input_project_head_id": "project-head-input",
        "successor_transition_id": "successor-source",
        "workspace_handoff_id": "workspace-handoff-source",
        "session_id": "session-source",
        "dataset_id": "dataset-source",
        "session_result_sha256": "1" * 64,
        "workspace_root": str(destination),
        "output_archive": archive.model_dump(mode="json"),
        "core_output_archive": archive.model_dump(mode="json"),
        "workspace_projection": projection,
    }
    authority = {
        **authority_body,
        "content_sha256": ports_module.canonical_sha256(authority_body),
    }
    root, verified = _require_successor_workspace_authority(
        authority=authority,
        run_root=run_root,
        task_id="Astronomy_004",
        attempt_index=1,
        project_id="project-successor",
    )
    assert root == destination.resolve()
    assert verified == archive

    with pytest.raises(
        CoreControlError,
        match="CANDIDATE_SUCCESSOR_WORKSPACE_AUTHORITY_INVALID",
    ):
        _require_successor_workspace_authority(
            authority=authority,
            run_root=run_root,
            task_id="Astronomy_004",
            attempt_index=2,
            project_id="project-successor",
        )


def test_candidate_successor_workspace_strips_sealed_runtime_only_entries(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    for relative in (
        "data",
        "related_work",
        "code",
        "outputs",
        "report",
        "artifacts",
        "skills",
        ".agents",
        ".codex",
        ".git",
    ):
        (source / relative).mkdir(parents=True, exist_ok=True)
    (source / "INSTRUCTIONS.md").write_text("closed task\n", encoding="utf-8")
    runtime_instruction = b"sealed evolved instruction\n"
    (source / "AGENTS.md").write_bytes(runtime_instruction)
    archive_path = tmp_path / "source-with-runtime.tar"
    descriptor = os.open(
        archive_path,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
        0o600,
    )
    try:
        raw_archive = write_workspace_archive(source, descriptor)
    finally:
        os.close(descriptor)
    runtime_receipt = {
        "files": [
            {
                "relative_path": "agent_system_targets/AGENTS.md",
                "size_bytes": len(runtime_instruction),
                "sha256": hashlib.sha256(runtime_instruction).hexdigest(),
            }
        ]
    }

    run_root = tmp_path / "run"
    destination = (
        run_root
        / "successor_workspaces"
        / "Astronomy_004"
        / "Astronomy_004_a1_v24"
    )
    materialized, projection = _materialize_successor_workspace(
        payload=archive_path.read_bytes(),
        destination=destination,
        expected_archive=raw_archive.model_dump(mode="json"),
        runtime_injection_receipt=runtime_receipt,
    )

    assert {item.name for item in destination.iterdir()} == {
        "INSTRUCTIONS.md",
        "data",
        "related_work",
        "code",
        "outputs",
        "report",
        "artifacts",
        "skills",
    }
    assert materialized.entry_count == raw_archive.entry_count - 4
    assert materialized.extracted_byte_size == (
        raw_archive.extracted_byte_size - len(runtime_instruction)
    )
    assert projection["core_output_archive"] == raw_archive.model_dump(mode="json")
    assert projection["sanitized_output_archive"] == materialized.model_dump(
        mode="json"
    )
    assert projection["stripped_entry_count"] == 4
    assert projection["stripped_byte_size"] == len(runtime_instruction)

    authority_body = {
        "schema_version": (
            "openevo.researchclawbench.successor_workspace_authority.v2"
        ),
        "task_id": "Astronomy_004",
        "source_attempt_index": 1,
        "source_run_id": "Astronomy_004_a1_v24",
        "project_id": "project-runtime-projection",
        "input_project_head_id": "project-head-input",
        "successor_transition_id": "successor-source",
        "workspace_handoff_id": "workspace-handoff-source",
        "session_id": "session-source",
        "dataset_id": "dataset-source",
        "session_result_sha256": "2" * 64,
        "workspace_root": str(destination),
        "output_archive": materialized.model_dump(mode="json"),
        "core_output_archive": raw_archive.model_dump(mode="json"),
        "workspace_projection": projection,
    }
    successor_authority = {
        **authority_body,
        "content_sha256": ports_module.canonical_sha256(authority_body),
    }
    root, verified = _require_successor_workspace_authority(
        authority=successor_authority,
        run_root=run_root,
        task_id="Astronomy_004",
        attempt_index=2,
        project_id="project-runtime-projection",
    )
    assert root == destination.resolve()
    assert verified == materialized
    core_archive, verified_projection = (
        _require_successor_core_workspace_projection(
            authority=successor_authority,
            sanitized_archive=verified,
        )
    )
    assert core_archive == raw_archive
    assert verified_projection == projection

    project = {"project_id": "project-runtime-projection", "state": "ready"}
    core_snapshot = _workspace_snapshot_for_archive(
        project_id=project["project_id"],
        archive=raw_archive,
    ).model_dump(mode="json")
    project_head = {
        "project_head_id": "project-head-runtime-projection",
        "manifest_sha256": "3" * 64,
        "workspace_snapshot": core_snapshot,
    }
    expected_snapshot = _require_existing_workspace_snapshot(
        project=project,
        project_head=project_head,
        archive=core_archive,
    )
    with pytest.raises(
        CoreControlError,
        match="CANDIDATE_EXISTING_WORKSPACE_SNAPSHOT_DRIFT",
    ):
        _require_existing_workspace_snapshot(
            project=project,
            project_head=project_head,
            archive=materialized,
        )
    binding = _candidate_workspace_binding_receipt(
        task_id="Astronomy_004",
        attempt_index=2,
        workspace_source="successor_workspace",
        workspace_root=destination,
        project=project,
        project_head=project_head,
        archive=materialized,
        core_archive=core_archive,
        workspace_projection=verified_projection,
        expected_snapshot=expected_snapshot,
        task_local_overlay_id="overlay-astronomy",
    )
    assert binding["schema_version"] == (
        "openevo.researchclawbench.candidate_workspace_binding.v2"
    )
    assert binding["workspace_snapshot_match"] is True
    assert binding["expected_workspace_snapshot"] == core_snapshot
    assert binding["sanitized_workspace_snapshot"] != core_snapshot

    run_id = "Astronomy_004_a2_v24"
    candidate_run_root = run_root / "runs" / run_id
    candidate_run_root.mkdir(parents=True)
    (candidate_run_root / "INSTRUCTIONS.md").write_text(
        "closed task\n", encoding="utf-8"
    )
    fallback_root = run_root / "sanitized_workspaces" / "Astronomy_004"
    shutil.copytree(destination, fallback_root)

    class Composites:
        def get_composite(self, _composite_id: str) -> dict[str, Any]:
            return {"core_project_head_id": project_head["project_head_id"]}

    class Client:
        def json(self, method: str, path: str) -> dict[str, Any]:
            assert method == "GET"
            assert path == "/v2/projects/project-runtime-projection"
            return {**project, "active_project_head": project_head}

    port = CoreV2CandidatePort(
        object(),  # type: ignore[arg-type]
        run_root,
        core_authority=object(),  # type: ignore[arg-type]
        composites=Composites(),  # type: ignore[arg-type]
    )
    preflight_binding = port._preflight_workspace_binding(
        Client(),  # type: ignore[arg-type]
        {
            "task_id": "Astronomy_004",
            "run_id": run_id,
            "attempt_index": 2,
            "core_project_id": project["project_id"],
            "input_composite_id": "composite-2",
            "successor_workspace_authority": successor_authority,
            "task_local_overlay_id": "overlay-astronomy",
        },
    )
    assert preflight_binding == binding

    with pytest.raises(ValueError, match="unsafe path"):
        _materialize_successor_workspace(
            payload=archive_path.read_bytes(),
            destination=tmp_path / "missing-receipt",
            expected_archive=raw_archive.model_dump(mode="json"),
        )
    with pytest.raises(ValueError, match="unsafe path"):
        _materialize_successor_workspace(
            payload=archive_path.read_bytes(),
            destination=tmp_path / "wrong-runtime-digest",
            expected_archive=raw_archive.model_dump(mode="json"),
            runtime_injection_receipt={
                "files": [
                    {
                        **runtime_receipt["files"][0],
                        "sha256": "0" * 64,
                    }
                ]
            },
        )

    polluted = tmp_path / "polluted"
    shutil.copytree(source, polluted)
    (polluted / ".git/config").write_text("unsafe\n", encoding="utf-8")
    polluted_archive_path = tmp_path / "polluted.tar"
    descriptor = os.open(
        polluted_archive_path,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
        0o600,
    )
    try:
        polluted_archive = write_workspace_archive(polluted, descriptor)
    finally:
        os.close(descriptor)
    with pytest.raises(ValueError, match="unsafe path"):
        _materialize_successor_workspace(
            payload=polluted_archive_path.read_bytes(),
            destination=tmp_path / "polluted-successor",
            expected_archive=polluted_archive.model_dump(mode="json"),
            runtime_injection_receipt=runtime_receipt,
        )


def test_candidate_successor_workspace_strips_empty_c000_runtime_directories(
    tmp_path: Path,
) -> None:
    source = tmp_path / "c000-source"
    for relative in (
        "data",
        "related_work",
        "code",
        "outputs",
        "report",
        "artifacts",
        "skills",
        ".agents",
        ".codex",
        ".git",
    ):
        (source / relative).mkdir(parents=True, exist_ok=True)
    (source / "INSTRUCTIONS.md").write_text("closed task\n", encoding="utf-8")
    archive_path = tmp_path / "c000-source.tar"
    descriptor = os.open(
        archive_path,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
        0o600,
    )
    try:
        raw_archive = write_workspace_archive(source, descriptor)
    finally:
        os.close(descriptor)

    destination = tmp_path / "c000-successor"
    materialized, projection = _materialize_successor_workspace(
        payload=archive_path.read_bytes(),
        destination=destination,
        expected_archive=raw_archive.model_dump(mode="json"),
    )

    assert {item.name for item in destination.iterdir()} == {
        "INSTRUCTIONS.md",
        "data",
        "related_work",
        "code",
        "outputs",
        "report",
        "artifacts",
        "skills",
    }
    assert materialized.entry_count == raw_archive.entry_count - 3
    assert materialized.extracted_byte_size == raw_archive.extracted_byte_size
    assert projection["stripped_entry_count"] == 3
    assert projection["stripped_byte_size"] == 0

    polluted = tmp_path / "c000-polluted"
    shutil.copytree(source, polluted)
    (polluted / ".codex/runtime.txt").write_text("unsafe\n", encoding="utf-8")
    polluted_archive_path = tmp_path / "c000-polluted.tar"
    descriptor = os.open(
        polluted_archive_path,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
        0o600,
    )
    try:
        polluted_archive = write_workspace_archive(polluted, descriptor)
    finally:
        os.close(descriptor)
    with pytest.raises(ValueError, match="unsafe path"):
        _materialize_successor_workspace(
            payload=polluted_archive_path.read_bytes(),
            destination=tmp_path / "c000-polluted-successor",
            expected_archive=polluted_archive.model_dump(mode="json"),
        )


def test_formal_v12_a1_static_template_reproduces_snapshot_drift() -> None:
    project = {
        "project_id": "project-e6eaa84e3a15c5a9c03afcc977a8ccc5",
        "state": "ready",
    }
    stale_a0_input = WorkspaceArchiveDeclarationV2(
        format="openevo_deterministic_tar_v1",
        media_type="application/vnd.openevo.workspace-tar",
        content_sha256=(
            "0124ab1d31f357987201083774fa6ae8ecbe4e70734e5ccf323d0325948424a7"
        ),
        byte_size=57395712,
        entry_count=15,
        extracted_byte_size=57385061,
    )
    sealed_a0_output = WorkspaceArchiveDeclarationV2(
        format="openevo_deterministic_tar_v1",
        media_type="application/vnd.openevo.workspace-tar",
        content_sha256=(
            "f90cd0cea626f9fa017f4021ef3a24cc3da1548db0068ca5c0d4d340af017c8d"
        ),
        byte_size=114139136,
        entry_count=29,
        extracted_byte_size=114117867,
    )
    active_snapshot = _workspace_snapshot_for_archive(
        project_id=project["project_id"],
        archive=sealed_a0_output,
    ).model_dump(mode="json")
    assert active_snapshot["workspace_snapshot_id"] == (
        "workspace-dc7bc2f1e1f04445eff663037e53fc53f1be0af0e7a0c430e63547d9e849a46c"
    )
    head = {
        "project_head_id": (
            "project-head-5348488f9eb1b779342d2f714aecbb0b9fa279fde013cb5978d1764fa33f5e53"
        ),
        "workspace_snapshot": active_snapshot,
    }
    with pytest.raises(
        CoreControlError,
        match="CANDIDATE_EXISTING_WORKSPACE_SNAPSHOT_DRIFT",
    ):
        _require_existing_workspace_snapshot(
            project=project,
            project_head=head,
            archive=stale_a0_input,
        )
    assert _require_existing_workspace_snapshot(
        project=project,
        project_head=head,
        archive=sealed_a0_output,
    ) == active_snapshot


def test_core_control_error_projection_preserves_v2_lowercase_code() -> None:
    assert _closed_core_control_error_code(
        {"code": "project_authority_changed", "message": "redacted"}
    ) == "project_authority_changed"


def test_candidate_runtime_injection_c000_is_exact_empty_generation_zero() -> None:
    head = _project_head(generation=0, artifact_count=0)
    authority = _candidate_runtime_injection_authority(
        session_result=_session_result(
            head,
            {
                "context_id": "context-empty",
                "context_injected": False,
                "context_source": "empty_genesis",
                "runtime_context_snapshot_id": "runtime-0",
            },
        ),
        project_head=head,
        selected_composite_id="c000",
        selected_composite=None,
    )
    assert authority["artifact_count"] == 0
    assert authority["runtime_injection_receipt"] is None
    assert authority["predecessor_project_head_id"] is None


def test_candidate_runtime_injection_binds_exact_triple_and_rejects_tamper() -> None:
    head = _project_head(generation=1, artifact_count=3)
    receipt, composite = _runtime_injection_fixture()
    session = _session_result(
        head,
        {
            "context_id": "context-runtime",
            "context_injected": True,
            "context_source": "materialized_successor",
            "runtime_context_snapshot_id": "runtime-1",
            "context_artifact_ids": [
                item["artifact_id"] for item in receipt["artifacts"]
            ],
            "runtime_injection_receipt": receipt,
        },
    )
    authority = _candidate_runtime_injection_authority(
        session_result=session,
        project_head=head,
        selected_composite_id="composite-1",
        selected_composite=composite,
    )
    assert authority["artifact_count"] == 3
    assert authority["runtime_injection_receipt_sha256"] == (
        ports_module.canonical_sha256(receipt)
    )
    projected = json.loads(json.dumps(session))
    del projected["metadata"]["evolution"]["context_artifact_ids"]
    projected_authority = _candidate_runtime_injection_authority(
        session_result=projected,
        project_head=head,
        selected_composite_id="composite-1",
        selected_composite=composite,
    )
    assert projected_authority["artifact_ids"] == [
        item["artifact_id"] for item in receipt["artifacts"]
    ]
    tampered = json.loads(json.dumps(session))
    tampered["metadata"]["evolution"]["runtime_injection_receipt"][
        "artifacts"
    ][0]["artifact_id"] = "registry-foreign"
    with pytest.raises(ValueError, match="selected composite"):
        _candidate_runtime_injection_authority(
            session_result=tampered,
            project_head=head,
            selected_composite_id="composite-1",
            selected_composite=composite,
        )
    missing_context = json.loads(json.dumps(session))
    missing_context["metadata"]["evolution"]["context_artifact_ids"] = []
    with pytest.raises(ValueError, match="context metadata"):
        _candidate_runtime_injection_authority(
            session_result=missing_context,
            project_head=head,
            selected_composite_id="composite-1",
            selected_composite=composite,
        )


def test_candidate_project_config_matches_native_closed_capabilities() -> None:
    config = ExperimentConfig.load(PROTOCOL)
    project = _project_config(config, CANARY_TASK, "Capability preflight.")
    assert all(
        selection.config == {"training_feedback_required": True}
        for target_id, selection in project.evolution.targets.items()
        if target_id != "parametric_memory"
    )
    snapshot = build_builtin_registry(
        ImplementationDistributionIdentity(
            distribution="openevo",
            distribution_version="0.0.0-test",
            distribution_digest="a" * 64,
        )
    )
    capabilities = build_evolution_capabilities(
        snapshot,
        profile=execution_profile_for_release_mode(
            "codex_subscription_transcript"
        ),
        audience=CapabilityAudience.DESKTOP,
        core_version="test",
    )
    _require_project_capabilities(
        capabilities.model_dump(mode="json"),
        project,
        expected_registry_digest=snapshot.registry_digest,
    )


def test_production_candidate_objective_always_applies_runtime_capture_safety(
    tmp_path: Path,
) -> None:
    instructions = tmp_path / "INSTRUCTIONS.md"
    instructions.write_text("OFFICIAL TASK\n", encoding="utf-8")

    fresh = _compose_candidate_objective(
        instructions,
        task_local_overlay=None,
    )
    evolved = _compose_candidate_objective(
        instructions,
        task_local_overlay={"recommended_actions": ["CURRENT TASK CORRECTION"]},
    )

    assert fresh.startswith("OFFICIAL TASK\n\n---\n\n## Runtime capture safety")
    assert "Never print,\ndump, or decode raw binary file bytes" in fresh
    assert evolved.startswith(fresh)
    assert (
        evolved.index("Runtime capture safety")
        < evolved.index("Workspace publication safety")
        < evolved.index("Deliverable validation contract")
        < evolved.index("CURRENT TASK CORRECTION")
    )
    assert "outside the current workspace" in fresh
    assert "no symbolic links, sockets, FIFOs, or device files" in fresh
    assert "all three semantic groups" in fresh
    assert "**Method**" in fresh
    assert "**Results**" in fresh
    assert "**Discussion**" in fresh


def test_candidate_project_capability_preflight_rejects_closed_config_drift() -> None:
    config = ExperimentConfig.load(PROTOCOL)
    payload = _project_config(config, CANARY_TASK, "Capability preflight.").model_dump(
        mode="json"
    )
    payload["evolution"]["targets"]["agent_system"]["config"] = {
        "adapter_only_undeclared_field": True
    }
    project = ports_module.ScienceProjectConfigV2.model_validate(payload)
    snapshot = build_builtin_registry(
        ImplementationDistributionIdentity(
            distribution="openevo",
            distribution_version="0.0.0-test",
            distribution_digest="b" * 64,
        )
    )
    capabilities = build_evolution_capabilities(
        snapshot,
        profile=execution_profile_for_release_mode(
            "codex_subscription_transcript"
        ),
        audience=CapabilityAudience.DESKTOP,
        core_version="test",
    )
    with pytest.raises(CoreControlError, match="METHOD_CONFIG_INVALID"):
        _require_project_capabilities(
            capabilities.model_dump(mode="json"),
            project,
            expected_registry_digest=snapshot.registry_digest,
        )


def test_candidate_waits_for_sealed_dataset_timeline_publication(monkeypatch) -> None:
    class Client:
        timeline_reads = 0

        def json(self, method, path, **_kwargs):
            assert method == "GET"
            if path.endswith("/timeline?limit=100"):
                self.timeline_reads += 1
                if self.timeline_reads == 1:
                    return {"items": []}
                return {
                    "items": [
                        {
                            "event_type": "dataset_sealed",
                            "attempt_id": "attempt-1",
                            "dataset_id": "dataset-1",
                        }
                    ]
                }
            if path == "/v2/tasks/task-1":
                return {"task_id": "task-1", "state": "waiting_for_successor"}
            assert path.endswith("/datasets/dataset-1")
            return {
                "completed_dataset_id": "dataset-1",
                "completed_dataset_revision": "artifact-1.v1",
                "session_id": "session-1",
            }

    sleeps = []
    monkeypatch.setattr(ports_module.time, "sleep", sleeps.append)
    dataset = _wait_for_completed_dataset(
        Client(),
        task_id="task-1",
        attempt_id="attempt-1",
        deadline=ports_module.time.monotonic() + 60,
    )
    assert dataset["session_id"] == "session-1"
    assert sleeps == [1.0]


def test_candidate_rejects_duplicate_sealed_dataset_events() -> None:
    class Client:
        def json(self, method, path, **_kwargs):
            assert method == "GET"
            assert path.endswith("/timeline?limit=100")
            return {
                "items": [
                    {
                        "event_type": "dataset_sealed",
                        "attempt_id": "attempt-1",
                        "dataset_id": dataset_id,
                    }
                    for dataset_id in ("dataset-1", "dataset-2")
                ]
            }

    with pytest.raises(CoreControlError, match="multiple completed datasets"):
        _wait_for_completed_dataset(
            Client(),
            task_id="task-1",
            attempt_id="attempt-1",
            deadline=ports_module.time.monotonic() + 60,
        )


def test_candidate_dataset_wait_fails_fast_on_terminal_successor() -> None:
    calls = []

    class Client:
        def json(self, method, path, **_kwargs):
            calls.append((method, path))
            assert method == "GET"
            if path.endswith("/timeline?limit=100"):
                return {"items": []}
            if path == "/v2/tasks/task-1":
                return {
                    "task_id": "task-1",
                    "state": "failed",
                    "successor_transition": {
                        "successor_transition_id": "transition-1",
                    },
                }
            assert path == (
                "/v2/internal/training-successors/transition-1"
            )
            return {
                "transition": {
                    "state": "failed",
                    "error": {"retryable": False},
                }
            }

    with pytest.raises(
        CoreControlError,
        match="terminal failure before dataset publication",
    ) as observed:
        _wait_for_completed_dataset(
            Client(),
            task_id="task-1",
            attempt_id="attempt-1",
            deadline=ports_module.time.monotonic() + 60,
        )

    assert observed.value.reason_code == (
        "candidate_successor_transition_failed"
    )
    assert calls == [
        ("GET", "/v2/tasks/task-1/timeline?limit=100"),
        ("GET", "/v2/tasks/task-1"),
        (
            "GET",
            "/v2/internal/training-successors/transition-1",
        ),
    ]


def test_candidate_retries_retryable_successor_once_before_dataset() -> None:
    calls = []
    timeline_reads = 0

    class Client:
        def json(self, method, path, **kwargs):
            nonlocal timeline_reads
            calls.append((method, path, kwargs))
            if path.endswith("/timeline?limit=100"):
                assert method == "GET"
                timeline_reads += 1
                if timeline_reads == 1:
                    return {"items": []}
                return {
                    "items": [
                        {
                            "event_type": "dataset_sealed",
                            "attempt_id": "attempt-1",
                            "dataset_id": "dataset-1",
                        }
                    ]
                }
            if path == "/v2/tasks/task-1":
                assert method == "GET"
                return {
                    "task_id": "task-1",
                    "state": "failed",
                    "successor_transition": {
                        "successor_transition_id": "transition-1",
                    },
                }
            if path == "/v2/internal/training-successors/transition-1":
                assert method == "GET"
                return {
                    "transition": {
                        "state": "failed",
                        "error": {"retryable": True},
                        "transition": {
                            "successor_transition_id": "transition-1",
                            "predecessor_project_head": {
                                "project_head_id": "head-1",
                            },
                        },
                    }
                }
            if path == "/v2/transitions/transition-1/retry":
                assert method == "POST"
                assert kwargs == {
                    "payload": {"expected_project_head_id": "head-1"},
                    "headers": {"Idempotency-Key": "candidate-1:dataset-retry"},
                }
                return {"state": "pending"}
            assert method == "GET"
            assert path.endswith("/datasets/dataset-1")
            return {
                "completed_dataset_id": "dataset-1",
                "completed_dataset_revision": "artifact-1.v1",
                "session_id": "session-1",
            }

    dataset = _wait_for_completed_dataset(
        Client(),
        task_id="task-1",
        attempt_id="attempt-1",
        deadline=ports_module.time.monotonic() + 60,
        pre_dataset_retry_idempotency_key="candidate-1:dataset-retry",
    )

    assert dataset["session_id"] == "session-1"
    assert [
        (method, path)
        for method, path, _kwargs in calls
        if method == "POST"
    ] == [("POST", "/v2/transitions/transition-1/retry")]


def test_candidate_terminal_setup_failure_converges_without_seal_wait() -> None:
    failure = {
        "schema_version": "openevo.science_attempt_failure_authority.v1",
        "task_id": "task-1",
        "attempt_id": "attempt-1",
        "failure_code": "candidate_subscription_isolation_not_ready",
        "retryable": False,
        "rollout_task_id": "rollout-attempt-1",
        "session_id": "sk-openevo-session-1",
        "session_status": "ERROR",
        "model_started": False,
        "benchmark_started": False,
        "benchmark_trace_count": 0,
    }
    failure["content_sha256"] = hashlib.sha256(
        json.dumps(
            failure,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    lifecycle = {
        "schema_version": "openevo.training_attempt_execution_status.v1",
        "task_id": "task-1",
        "attempt_id": "attempt-1",
        "state": "failed",
        "terminal": True,
        "captured": False,
        "error_code": "candidate_subscription_isolation_not_ready",
        "retryable": False,
        "model_started": False,
        "benchmark_started": False,
        "failure_authority": failure,
    }
    with pytest.raises(CandidateAuthorityUnavailable) as observed:
        require_nonterminal_candidate_lifecycle(
            lifecycle,
            task_id="task-1",
            attempt_id="attempt-1",
        )
    assert observed.value.terminal_proven is True
    assert observed.value.reason_code == (
        "candidate_subscription_isolation_not_ready"
    )
    assert observed.value.failure_receipt == {
        "schema_version": "openevo.candidate_terminal_failure.v1",
        "core_task_id": "task-1",
        "core_attempt_id": "attempt-1",
        "underlying_session_status": "ERROR",
        "model_started": False,
        "benchmark_started": False,
        "retryable": False,
        "core_failure_code": "candidate_subscription_isolation_not_ready",
        "terminal_proven": True,
    }


def test_candidate_terminal_failure_without_authority_fails_closed() -> None:
    with pytest.raises(
        CoreControlError,
        match="CANDIDATE_TERMINAL_FAILURE_AUTHORITY_INVALID",
    ):
        require_nonterminal_candidate_lifecycle(
            {
                "task_id": "task-1",
                "attempt_id": "attempt-1",
                "state": "failed",
                "terminal": True,
                "captured": False,
                "error_code": "candidate_execution_failed",
            },
            task_id="task-1",
            attempt_id="attempt-1",
        )


def _build(tmp_path: Path, monkeypatch, *, crash_point: str | None = None):
    monkeypatch.setenv("JUDGE_API_KEY", "synthetic-present")
    monkeypatch.setenv("JUDGE_API_BASE", "https://synthetic.invalid")
    monkeypatch.setenv("JUDGE_MODEL_NAME", "synthetic-no-model")
    config = ExperimentConfig.load(PROTOCOL)
    ports, authorities = build_synthetic_ports(tmp_path / "external", crash_point=crash_point)
    operations = ProductionTrainingOperations(
        config=config,
        experiment_run_id="rcb_oe_v0_synthetic_life_three",
        receipt_root=tmp_path / "operations",
        ports=ports,
    )
    store = TrainingStateStore(tmp_path / "supervisor")
    supervisor = CommunityTrainingSupervisor(
        store=store,
        experiment_id="rcb_oe_v0_synthetic_life_three",
        identity=_identity(config),
        operations=operations,
        task_ids=(CANARY_TASK,),
    )
    return config, supervisor, authorities


def _drive(supervisor: CommunityTrainingSupervisor, limit: int = 200):
    for _ in range(limit):
        state = supervisor.status()
        if state["stage"] == TrainingStage.FINAL_FROZEN.value:
            return state
        supervisor.run_next()
    raise AssertionError("synthetic supervisor did not terminate")


def test_typed_operation_receipt_is_stable_and_recovered(tmp_path: Path, monkeypatch) -> None:
    config, _supervisor, _authorities = _build(tmp_path, monkeypatch)
    operations = ProductionTrainingOperations(
        config=config,
        experiment_run_id="rcb_oe_v0_receipt_test",
        receipt_root=tmp_path / "receipt-test",
        ports=build_synthetic_ports(tmp_path / "receipt-external")[0],
    )
    request = {
        "experiment_id": "rcb_oe_v0_receipt_test",
        "task_id": CANARY_TASK,
        "attempt_index": 0,
        "run_id": "Life_005_a0_v2",
        "core_project_id": None,
        "fresh_workspace": True,
        "resume_in_place": False,
    }
    first = operations.start_candidate(request, "rcb_oe_v0_receipt_test:candidate")
    second = operations.start_candidate(request, "rcb_oe_v0_receipt_test:candidate")
    assert first == second
    assert first.status is OperationStatus.SUCCEEDED
    assert Path(first.receipt_path).is_file()
    assert first.content_sha256 == second.content_sha256


def test_missing_judge_stops_before_evaluator_side_effect(tmp_path: Path, monkeypatch) -> None:
    config, _supervisor, _authorities = _build(tmp_path, monkeypatch)
    for name in ("JUDGE_API_KEY", "JUDGE_API_BASE", "JUDGE_MODEL_NAME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        operations_module,
        "judge_credential_readiness",
        lambda _config: {"ready": False},
    )
    operations = ProductionTrainingOperations(
        config=config,
        experiment_run_id="rcb_oe_v0_missing_judge",
        receipt_root=tmp_path / "missing-judge-operations",
        ports=build_synthetic_ports(tmp_path / "missing-judge-external")[0],
    )
    with pytest.raises(JudgeCredentialsRequired, match="JUDGE_CREDENTIALS_REQUIRED"):
        operations.evaluate_candidate(
            {"task_id": CANARY_TASK, "candidate": {}, "validation": {}},
            "rcb_oe_v0_missing_judge:evaluation",
        )


def test_judge_identity_preflight_uses_evaluator_only_child_without_request(
    tmp_path: Path,
    monkeypatch,
) -> None:
    dependency_path = tmp_path / "runtime/evaluator-dependency-lock.json"
    dependency = write_evaluator_dependency_lock(dependency_path)
    dependency_claim = {
        "path": str(dependency_path),
        "sha256": hashlib.sha256(dependency_path.read_bytes()).hexdigest(),
        "content_sha256": dependency["content_sha256"],
    }

    class Config:
        project_root = ROOT
        experiment_root = tmp_path

        @staticmethod
        def require(key: str):
            return {
                "judge.model": "openai/gpt-5.1",
                "judge.provider": "openai_compatible",
                "judge.evaluator_dependency_lock": dependency_claim,
            }[key]

    for name in ("JUDGE_API_KEY", "JUDGE_API_BASE", "JUDGE_MODEL_NAME"):
        monkeypatch.delenv(name, raising=False)
    secret_root = tmp_path / "secrets"
    secret_root.mkdir(mode=0o700)
    secret = secret_root / "judge.env"
    secret.write_text(
        "JUDGE_API_KEY=synthetic-not-a-real-key\n"
        "JUDGE_API_BASE=https://openrouter.ai/api/v1\n"
        "JUDGE_MODEL_NAME=openai/gpt-5.1\n",
        encoding="utf-8",
    )
    secret.chmod(0o600)
    destination = tmp_path / "formal_preflight/evaluator_private/judge.json"
    first = judge_identity_preflight(Config(), receipt_path=destination)
    second = judge_identity_preflight(Config(), receipt_path=destination)
    assert first == second
    assert first == {
        "schema_version": "openevo.researchclawbench.judge_scorer_preflight.v2",
        "api_base": "https://openrouter.ai/api/v1",
        "api_key_present": True,
        "model": "openai/gpt-5.1",
        "provider": "openai_compatible",
        "scorer_import_ready": True,
        "scorer_module_origin": "ResearchClawBench/evaluation/score.py",
        "structai_version": "0.1.23",
        "evaluator_dependency_lock_sha256": dependency_claim["sha256"],
        "evaluator_dependency_content_sha256": dependency["content_sha256"],
        "tasks_dir_authority_matches": True,
        "tasks_dir_relative": "ResearchClawBench/tasks",
        "model_slug_preserved": True,
        "judge_request_started": False,
        "model_started": False,
        "secret_recorded": False,
    }
    assert "synthetic-not-a-real-key" not in destination.read_text(encoding="utf-8")


def test_judge_identity_preflight_rejects_model_drift_without_request(
    tmp_path: Path,
    monkeypatch,
) -> None:
    dependency_path = tmp_path / "runtime/evaluator-dependency-lock.json"
    dependency = write_evaluator_dependency_lock(dependency_path)
    dependency_claim = {
        "path": str(dependency_path),
        "sha256": hashlib.sha256(dependency_path.read_bytes()).hexdigest(),
        "content_sha256": dependency["content_sha256"],
    }

    class Config:
        project_root = ROOT
        experiment_root = tmp_path

        @staticmethod
        def require(key: str):
            return {
                "judge.model": "openai/gpt-5.1",
                "judge.provider": "openai_compatible",
                "judge.evaluator_dependency_lock": dependency_claim,
            }[key]

    for name in ("JUDGE_API_KEY", "JUDGE_API_BASE", "JUDGE_MODEL_NAME"):
        monkeypatch.delenv(name, raising=False)
    secret_root = tmp_path / "secrets"
    secret_root.mkdir(mode=0o700)
    secret = secret_root / "judge.env"
    secret.write_text(
        "JUDGE_API_KEY=synthetic-not-a-real-key\n"
        "JUDGE_API_BASE=https://openrouter.ai/api/v1\n"
        "JUDGE_MODEL_NAME=gpt-5.1\n",
        encoding="utf-8",
    )
    secret.chmod(0o600)
    destination = tmp_path / "formal_preflight/evaluator_private/judge.json"
    with pytest.raises(JudgeCredentialsRequired, match="failed closed"):
        judge_identity_preflight(Config(), receipt_path=destination)
    assert not destination.exists()


def test_task_local_overlay_requires_same_task_sealed_core_authority() -> None:
    payload, digest = _closed_task_local_overlay(
        {
            "status": "sealed",
            "task_scope_id": "core-task-1",
            "task_local_feedback": {
                "benchmark_task_scope_id": CANARY_TASK,
                "expected": "same-task-only",
            },
        },
        task_id=CANARY_TASK,
        core_task_scope_id="core-task-1",
    )
    assert payload == {"expected": "same-task-only"}
    assert len(digest) == 64
    with pytest.raises(ValueError, match="same-task sealed Core authority"):
        _closed_task_local_overlay(
            {
                "status": "sealed",
                "task_scope_id": "core-task-1",
                "task_local_feedback": {
                    "benchmark_task_scope_id": "Life_006",
                    "expected": "wrong-task",
                },
            },
            task_id=CANARY_TASK,
            core_task_scope_id="core-task-1",
        )


def test_feedback_overlay_identity_is_attachment_not_unresolvable_hash() -> None:
    class Client:
        def json(self, method, path, *, payload=None):
            if method == "GET":
                assert path.endswith("/datasets/dataset-1")
                return {
                    "completed_dataset_id": "dataset-1",
                    "completed_dataset_revision": "dataset-1.v1",
                    "session_id": "session-1",
                    "task_id": "rollout-attempt-1",
                }
            assert method == "POST"
            assert payload["task_id"] == "rollout-attempt-1"
            assert payload["task_scope_id"] == "core-task-1"
            return {
                "resolved_view_sha256": "a" * 64,
                "dataset_artifact": {"artifact_id": "resolved-dataset"},
                "dataset_view": {"task_local_overlay_sha256": "b" * 64},
            }

    result = CoreFeedbackPort._resolve(
        Client(),
        {
            "dataset_id": "dataset-1",
            "dataset_revision": "dataset-1.v1",
            "session_id": "session-1",
            "core_task_id": "core-task-1",
            "task_id": CANARY_TASK,
        },
        {
            "attachment_id": "attachment-1",
            "content_sha256": "c" * 64,
            "task_local_feedback": {"expected": "same-task-only"},
        },
    )
    assert result["task_local_overlay_id"] == "attachment-1"
    assert result["task_local_overlay_sha256"] == "b" * 64


def test_feedback_uses_completed_dataset_task_identity_not_science_task_id() -> None:
    calls = []

    class Client:
        def json(self, method, path, *, payload=None):
            calls.append((method, path, payload))
            if method == "GET":
                return {
                    "completed_dataset_id": "dataset-1",
                    "completed_dataset_revision": "artifact-1.v1",
                    "session_id": "session-1",
                    "task_id": "rollout-attempt-1",
                }
            raise AssertionError("only the authority read was expected")

    authority = CoreFeedbackPort._completed_dataset_authority(
        Client(),
        {
            "dataset_id": "dataset-1",
            "dataset_revision": "artifact-1.v1",
            "session_id": "session-1",
            "core_task_id": "science-task-1",
        },
    )
    assert authority["task_id"] == "rollout-attempt-1"
    assert authority["task_id"] != "science-task-1"
    assert calls[0][1].endswith("/datasets/dataset-1")


def test_feedback_attachment_separates_dataset_science_and_benchmark_scopes(
    tmp_path: Path,
) -> None:
    calls = []

    class Client:
        def json(self, method, path, *, payload=None, headers=None):
            del headers
            calls.append((method, path, payload))
            if method == "GET":
                return {
                    "completed_dataset_id": "dataset-1",
                    "completed_dataset_revision": "artifact-1.v1",
                    "session_id": "session-1",
                    "task_id": "rollout-attempt-1",
                }
            if path.endswith("/attachments"):
                assert payload["task_id"] == "rollout-attempt-1"
                assert payload["task_scope_id"] == "science-task-1"
                assert (
                    payload["task_local_feedback"]["benchmark_task_scope_id"]
                    == CANARY_TASK
                )
                return {
                    **payload,
                    "attachment_id": "attachment-1",
                    "content_sha256": "c" * 64,
                    "status": "sealed",
                }
            assert path.endswith("/resolve")
            assert payload["task_id"] == "rollout-attempt-1"
            assert payload["task_scope_id"] == "science-task-1"
            return {
                "resolved_view_sha256": "a" * 64,
                "dataset_artifact": {"artifact_id": "resolved-dataset"},
                "dataset_view": {"task_local_overlay_sha256": "b" * 64},
            }

    class Port(CoreFeedbackPort):
        @contextmanager
        def _client(self):
            yield Client()

    run_root = tmp_path / "run"
    (run_root / "report").mkdir(parents=True)
    (run_root / "report" / "report.md").write_text(
        "# Summary\n## Findings\n", encoding="utf-8"
    )
    result = Port(core_authority=object()).execute(
        {
            "task_id": CANARY_TASK,
            "core_task_id": "science-task-1",
            "dataset_id": "dataset-1",
            "dataset_revision": "artifact-1.v1",
            "session_id": "session-1",
            "candidate": {"candidate_output_root": str(run_root)},
            "validation": {
                "artifact_valid": False,
                "completeness": 10,
                "validator_errors": ["REPORT_REQUIRED_SECTIONS_MISSING"],
            },
            "feedback_source": "artifact_validator",
        },
        "supervisor-feedback-key",
    )
    assert result["task_local_overlay_scope_id"] == "science-task-1"
    assert result["feedback_source"] == "artifact_validator"
    assert len(calls) == 3


def test_soft_judge_attachment_projects_out_verified_routing_metadata() -> None:
    calls = []

    class Evaluator:
        def read_feedback_for_attachment(self, *, idempotency_key, expected_sha256):
            assert idempotency_key == "judge-operation-key"
            assert expected_sha256 == "f" * 64
            return {
                "feedback_class": "SOFT_JUDGE",
                "global_feedback": {
                    "task_id": CANARY_TASK,
                    "attempt_id": "Life_005_a0_v26",
                    "completed": True,
                    "exit_code": 0,
                    "artifact_valid": True,
                    "total_score": 0.0,
                    "generic_failure_tags": [],
                    "runtime_bucket_seconds": 600,
                    "cost_total_usd": "unavailable",
                    "artifact_root_sha256": "e" * 64,
                },
                "task_local_feedback": {},
            }

    class Client:
        def json(self, method, path, *, payload=None, headers=None):
            del headers
            calls.append((method, path, payload))
            if method == "GET":
                return {
                    "completed_dataset_id": "dataset-1",
                    "completed_dataset_revision": "artifact-1.v1",
                    "session_id": "session-1",
                    "task_id": "rollout-attempt-1",
                }
            if path.endswith("/attachments"):
                assert "task_id" not in payload["global_feedback"]
                assert "attempt_id" not in payload["global_feedback"]
                return {
                    **payload,
                    "attachment_id": "attachment-1",
                    "content_sha256": "c" * 64,
                    "status": "sealed",
                }
            assert path.endswith("/resolve")
            return {
                "resolved_view_sha256": "a" * 64,
                "dataset_artifact": {"artifact_id": "resolved-dataset"},
                "dataset_view": {"task_local_overlay_sha256": None},
            }

    class Port(CoreFeedbackPort):
        @contextmanager
        def _client(self):
            yield Client()

    result = Port(core_authority=object(), evaluator=Evaluator()).execute(
        {
            "task_id": CANARY_TASK,
            "core_task_id": "science-task-1",
            "dataset_id": "dataset-1",
            "dataset_revision": "artifact-1.v1",
            "session_id": "session-1",
            "evaluation": {
                "idempotency_key": "judge-operation-key",
                "private_feedback_sha256": "f" * 64,
                "feedback_class": "SOFT_JUDGE",
                "attempt_id": "Life_005_a0_v26",
            },
        },
        "supervisor-feedback-key",
    )
    assert result["feedback_class"] == "SOFT_JUDGE"
    assert result["task_local_overlay_id"] is None
    assert len(calls) == 3


def test_current_task_gt_attachment_uses_core_without_judge() -> None:
    calls = []

    class Client:
        def json(self, method, path, *, payload=None, headers=None):
            del headers
            calls.append((method, path, payload))
            if method == "GET":
                return {
                    "completed_dataset_id": "dataset-gt",
                    "completed_dataset_revision": "artifact-gt.v1",
                    "session_id": "session-gt",
                    "task_id": "rollout-gt",
                }
            if path.endswith("/attachments"):
                assert payload["feedback_class"] == "HARD_GT"
                assert payload["global_feedback"] == {}
                assert payload["task_local_feedback"]["feedback_source"] == (
                    "current_task_gt"
                )
                return {
                    **payload,
                    "attachment_id": "attachment-gt",
                    "content_sha256": "c" * 64,
                    "status": "sealed",
                }
            assert path.endswith("/resolve")
            return {
                "resolved_view_sha256": "a" * 64,
                "dataset_artifact": {"artifact_id": "resolved-gt"},
                "dataset_view": {"task_local_overlay_sha256": "b" * 64},
            }

    class Port(CoreFeedbackPort):
        @contextmanager
        def _client(self):
            yield Client()

    result = Port(core_authority=object(), evaluator=None).execute(
        {
            "task_id": CANARY_TASK,
            "core_task_id": "science-task-gt",
            "dataset_id": "dataset-gt",
            "dataset_revision": "artifact-gt.v1",
            "session_id": "session-gt",
            "feedback_source": "current_task_gt",
            "gt_supervision": {
                "task_id": CANARY_TASK,
                "ground_truth_sha256": "7" * 64,
                "feedback_class": "HARD_GT",
                "global_feedback": {},
                "task_local_feedback": {
                    "feedback_source": "current_task_gt",
                    "ground_truth_sha256": "7" * 64,
                    "ground_truth_entries": [{"content": "expected"}],
                    "judge_feedback_included": False,
                },
                "judge_feedback_included": False,
            },
        },
        "supervisor-current-task-gt-key",
    )

    assert result["feedback_class"] == "HARD_GT"
    assert result["feedback_source"] == "current_task_gt"
    assert result["judge_calls"] == 0
    assert result["judge_feedback_included"] is False
    assert result["ground_truth_sha256"] == "7" * 64
    assert len(calls) == 3


class _SuccessorCoreAuthority:
    generation = "1" * 32
    release_identity = "2" * 64


def _successor_request() -> dict[str, Any]:
    return {
        "attachment": {
            "successor_transition_id": "transition-failed",
            "attachment_id": "attachment-1",
            "resolved_view_sha256": "a" * 64,
        }
    }


def _failed_successor_authority(
    *,
    retryable: bool,
    commit: dict[str, Any] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    error = {
        "code": "successor_transition_failed",
        "retryable": retryable,
    }
    return {
        "transition": {
            "state": "failed",
            "error": error,
            "transition": {
                "successor_transition_id": "transition-failed",
                "predecessor_project_head": {
                    "project_head_id": "head-0",
                    "manifest_sha256": "b" * 64,
                },
            },
        },
        "attempts": [
            {
                "transition_attempt_id": "transition-attempt-1",
                "successor_transition_id": "transition-failed",
                "state": "failed",
                "error": error,
            }
        ],
        "commit": commit,
        "artifacts": [] if artifacts is None else artifacts,
    }


def test_successor_nonretryable_failure_is_never_resubmitted(monkeypatch) -> None:
    calls = []

    class Client:
        def __init__(self, _authority):
            pass

        def close(self):
            pass

        def json(self, method, path, *, payload=None, headers=None):
            calls.append((method, path, payload, headers))
            return _failed_successor_authority(retryable=False)

    monkeypatch.setattr(ports_module, "CoreControlV2Client", Client)
    port = CoreSuccessorPort(object(), core_authority=_SuccessorCoreAuthority())
    with pytest.raises(SuccessorRecoveryRequired) as raised:
        port.execute(_successor_request(), "retry-key")
    assert [call[0] for call in calls] == ["GET"]
    assert raised.value.checkpoint["commit_absent"] is True
    assert raised.value.checkpoint["successor_artifact_count"] == 0
    assert raised.value.checkpoint["supervisor_idempotency_key"] == "retry-key"


def test_successor_retryable_failure_submits_one_retry_then_fails_closed(
    monkeypatch,
) -> None:
    calls = []

    class Client:
        def __init__(self, _authority):
            pass

        def close(self):
            pass

        def json(self, method, path, *, payload=None, headers=None):
            calls.append((method, path, payload, headers))
            if method == "POST":
                return {"operation_id": "retry-1"}
            get_count = sum(call[0] == "GET" for call in calls)
            return _failed_successor_authority(retryable=get_count == 1)

    monkeypatch.setattr(ports_module, "CoreControlV2Client", Client)
    port = CoreSuccessorPort(object(), core_authority=_SuccessorCoreAuthority())
    with pytest.raises(SuccessorRecoveryRequired):
        port.execute(_successor_request(), "retry-key")
    assert [call[0] for call in calls] == ["GET", "POST", "GET"]
    assert calls[1][3] == {"Idempotency-Key": "retry-key"}


@pytest.mark.parametrize(
    ("commit", "artifacts"),
    [
        ({"commit_id": "commit-1"}, []),
        (None, [{"artifact_id": "artifact-1"}]),
    ],
)
def test_successor_terminal_checkpoint_rejects_commit_or_artifact_conflict(
    commit,
    artifacts,
) -> None:
    port = CoreSuccessorPort(object(), core_authority=_SuccessorCoreAuthority())
    with pytest.raises(CoreControlError, match="conflicts"):
        port._recovery_checkpoint(
            _failed_successor_authority(
                retryable=False,
                commit=commit,
                artifacts=artifacts,
            ),
            request=_successor_request(),
            idempotency_key="retry-key",
        )


def test_successor_lost_retry_response_reconciles_commit_without_second_post(
    monkeypatch,
) -> None:
    calls = []

    class Client:
        committed = False

        def __init__(self, _authority):
            pass

        def close(self):
            pass

        def json(self, method, path, *, payload=None, headers=None):
            calls.append((method, path, payload, headers))
            if method == "POST":
                type(self).committed = True
                raise ports_module.httpx.ReadError("lost retry response")
            if type(self).committed:
                return {"transition": {"state": "committed"}}
            return _failed_successor_authority(retryable=True)

    monkeypatch.setattr(ports_module, "CoreControlV2Client", Client)
    monkeypatch.setattr(
        CoreSuccessorPort,
        "_result",
        staticmethod(
            lambda _authority, _request, _client: {
                "successor_commit_id": "commit-1"
            }
        ),
    )
    port = CoreSuccessorPort(object(), core_authority=_SuccessorCoreAuthority())
    with pytest.raises(ports_module.httpx.ReadError, match="lost retry response"):
        port.execute(_successor_request(), "retry-key")
    recovered = port.recover(_successor_request(), "retry-key")
    assert recovered == {"successor_commit_id": "commit-1"}
    assert [call[0] for call in calls] == ["GET", "POST", "GET"]


def test_completed_method_reconciliation_lost_response_reads_commit_without_second_post(
    monkeypatch,
) -> None:
    source = _failed_successor_authority(retryable=False)
    checkpoint = CoreSuccessorPort(
        object(), core_authority=_SuccessorCoreAuthority()
    )._recovery_checkpoint(
        source,
        request=_successor_request(),
        idempotency_key="completed-method-lost-response-key",
    )
    reconciliation_id = "successor-reconcile-" + "a" * 40
    calls = []

    class Client:
        committed = False

        def __init__(self, _authority):
            pass

        def close(self):
            pass

        def readiness(self):
            return {
                "generation_matches": True,
                "secret_recorded": False,
            }

        def json(self, method, path, *, payload=None, headers=None):
            calls.append((method, path, payload, headers))
            if method == "POST":
                type(self).committed = True
                raise ports_module.httpx.ReadError(
                    "lost completed-method reconciliation response"
                )
            if type(self).committed:
                return {
                    "transition": {
                        **source["transition"],
                        "state": "committed",
                        "error": None,
                    },
                    "attempts": [
                        *source["attempts"],
                        {
                            "transition_attempt_id": "reconciliation-attempt-2",
                            "successor_transition_id": "transition-failed",
                            "retry_request_id": reconciliation_id,
                            "reconciliation_only": True,
                            "state": "committed",
                            "error": None,
                        },
                    ],
                    "commit": {"commit_id": "commit-reconciled-1"},
                    "artifacts": [],
                }
            return source

    monkeypatch.setattr(ports_module, "CoreControlV2Client", Client)
    monkeypatch.setattr(
        CoreSuccessorPort,
        "_result",
        staticmethod(
            lambda _authority, _request, _client: {
                "successor_commit_id": "commit-reconciled-1",
                "model_calls_started": 0,
            }
        ),
    )
    port = CoreSuccessorPort(
        object(),
        core_authority=_SuccessorCoreAuthority(),
    )

    with pytest.raises(
        ports_module.httpx.ReadError,
        match="lost completed-method reconciliation response",
    ):
        port.reconcile_terminal_successor(
            _successor_request(),
            "completed-method-lost-response-key",
            checkpoint,
            reconciliation_id,
        )
    recovered = port.reconcile_terminal_successor(
        _successor_request(),
        "completed-method-lost-response-key",
        checkpoint,
        reconciliation_id,
    )

    assert recovered["successor_commit_id"] == "commit-reconciled-1"
    assert recovered["model_calls_started"] == 0
    assert recovered["completed_methods_reconciliation"] == {
        "schema_version": (
            "openevo.researchclawbench.completed_methods_reconciliation.v1"
        ),
        "reconciliation_id": reconciliation_id,
        "successor_transition_id": "transition-failed",
        "source_transition_attempt_count": len(source["attempts"]),
        "source_terminal_authority_sha256": checkpoint[
            "terminal_authority_sha256"
        ],
        "core_generation": "1" * 32,
        "core_release_identity": "2" * 64,
        "reconciliation_only": True,
        "model_execution_allowed": False,
        "model_calls_started": 0,
    }
    assert [call[0] for call in calls] == ["GET", "POST", "GET"]


def test_runner_restart_replays_terminal_successor_checkpoint_without_side_effect(
    tmp_path: Path,
    monkeypatch,
) -> None:
    del monkeypatch
    checkpoint = CoreSuccessorPort(
        object(), core_authority=_SuccessorCoreAuthority()
    )._recovery_checkpoint(
        _failed_successor_authority(retryable=False),
        request=_successor_request(),
        idempotency_key="checkpoint-key",
    )

    class FailingPort:
        recover_calls = 0
        execute_calls = 0

        def recover(self, request, idempotency_key):
            type(self).recover_calls += 1

        def execute(self, request, idempotency_key):
            type(self).execute_calls += 1
            raise SuccessorRecoveryRequired(checkpoint)

    first_port = FailingPort()
    base_ports = ProductionPorts(
        candidate=first_port,
        validation=first_port,
        evaluation=first_port,
        attachment=first_port,
        evolution=first_port,
        composite=first_port,
        task_local=first_port,
        sanitizer=first_port,
        freeze=first_port,
    )
    receipt_root = tmp_path / "checkpoint-operations"
    operations = ProductionTrainingOperations(
        config=object(),
        experiment_run_id="rcb_oe_v0_checkpoint_test",
        receipt_root=receipt_root,
        ports=base_ports,
    )
    request = {"task_id": CANARY_TASK, **_successor_request()}
    with pytest.raises(SuccessorRecoveryRequired) as first:
        operations.collect_artifact_jobs(request, "checkpoint-key")
    receipt = operations.receipts.get("checkpoint-key")
    assert receipt is not None
    assert receipt["status"] == OperationStatus.FAILED_TERMINAL.value
    assert receipt["failure_class"] == FailureClass.AUTHORITY.value

    class BombPort:
        calls = 0

        def recover(self, request, idempotency_key):
            type(self).calls += 1
            raise AssertionError("external recovery was replayed")

        execute = recover

    restarted = ProductionTrainingOperations(
        config=object(),
        experiment_run_id="rcb_oe_v0_checkpoint_test",
        receipt_root=receipt_root,
        ports=ProductionPorts(
            candidate=BombPort(),
            validation=BombPort(),
            evaluation=BombPort(),
            attachment=BombPort(),
            evolution=BombPort(),
            composite=BombPort(),
            task_local=BombPort(),
            sanitizer=BombPort(),
            freeze=BombPort(),
        ),
    )
    with pytest.raises(SuccessorRecoveryRequired) as second:
        restarted.collect_artifact_jobs(request, "checkpoint-key")
    assert first.value.checkpoint == second.value.checkpoint
    assert first.value.checkpoint["core_generation"] == "1" * 32
    assert FailingPort.recover_calls == 1
    assert FailingPort.execute_calls == 1
    assert BombPort.calls == 0


def test_terminal_successor_reconciliation_preserves_source_and_replays_without_calls(
    tmp_path: Path,
) -> None:
    checkpoint = CoreSuccessorPort(
        object(), core_authority=_SuccessorCoreAuthority()
    )._recovery_checkpoint(
        _failed_successor_authority(retryable=False),
        request=_successor_request(),
        idempotency_key="completed-method-checkpoint-key",
    )

    class TerminalPort:
        def recover(self, request, idempotency_key):
            del request, idempotency_key

        def execute(self, request, idempotency_key):
            del request, idempotency_key
            raise SuccessorRecoveryRequired(checkpoint)

    receipt_root = tmp_path / "completed-method-reconciliation"
    terminal_port = TerminalPort()
    terminal_operations = ProductionTrainingOperations(
        config=object(),
        experiment_run_id="rcb_oe_v0_completed_method_reconciliation",
        receipt_root=receipt_root,
        ports=ProductionPorts(
            candidate=terminal_port,
            validation=terminal_port,
            evaluation=terminal_port,
            attachment=terminal_port,
            evolution=terminal_port,
            composite=terminal_port,
            task_local=terminal_port,
            sanitizer=terminal_port,
            freeze=terminal_port,
        ),
    )
    request = {"task_id": CANARY_TASK, **_successor_request()}
    with pytest.raises(SuccessorRecoveryRequired):
        terminal_operations.collect_artifact_jobs(
            request,
            "completed-method-checkpoint-key",
        )
    source_path = terminal_operations.receipts._path(
        "completed-method-checkpoint-key"
    )
    source_before = source_path.read_bytes()
    source_receipt = terminal_operations.receipts.get(
        "completed-method-checkpoint-key"
    )
    assert source_receipt is not None

    class ReconciliationPort:
        calls = 0

        def recover(self, request, idempotency_key):
            raise AssertionError((request, idempotency_key))

        def execute(self, request, idempotency_key):
            raise AssertionError((request, idempotency_key))

        def reconcile_terminal_successor(
            self,
            observed_request,
            idempotency_key,
            observed_checkpoint,
            reconciliation_id,
        ):
            type(self).calls += 1
            assert observed_request == {
                **request,
                "operation_phase": "collect",
            }
            assert idempotency_key == "completed-method-checkpoint-key"
            assert observed_checkpoint == checkpoint
            assert reconciliation_id.startswith("successor-reconcile-")
            return {
                "owned_resource_ids": ["successor-commit-reconciled-1"],
                "successor_commit_id": "successor-commit-reconciled-1",
                "model_calls_started": 0,
            }

    reconciliation_port = ReconciliationPort()
    reconciled_operations = ProductionTrainingOperations(
        config=object(),
        experiment_run_id="rcb_oe_v0_completed_method_reconciliation",
        receipt_root=receipt_root,
        ports=ProductionPorts(
            candidate=reconciliation_port,
            validation=reconciliation_port,
            evaluation=reconciliation_port,
            attachment=reconciliation_port,
            evolution=reconciliation_port,
            composite=reconciliation_port,
            task_local=reconciliation_port,
            sanitizer=reconciliation_port,
            freeze=reconciliation_port,
        ),
    )
    recovered = reconciled_operations.collect_artifact_jobs(
        request,
        "completed-method-checkpoint-key",
    )
    assert recovered.status is OperationStatus.RECOVERED
    assert recovered.payload["successor_commit_id"] == (
        "successor-commit-reconciled-1"
    )
    assert recovered.payload["model_calls_started"] == 0
    assert recovered.payload["source_terminal_receipt_sha256"] == (
        source_receipt["content_sha256"]
    )
    assert source_path.read_bytes() == source_before
    assert Path(recovered.receipt_path).is_file()
    assert Path(recovered.receipt_path) != source_path
    assert len(tuple(receipt_root.glob("*.json"))) == 2
    assert ReconciliationPort.calls == 1

    replayed = reconciled_operations.collect_artifact_jobs(
        request,
        "completed-method-checkpoint-key",
    )
    assert replayed == recovered
    assert source_path.read_bytes() == source_before
    assert ReconciliationPort.calls == 1


def test_feedback_rejects_completed_dataset_authority_drift() -> None:
    class Client:
        def json(self, *_args, **_kwargs):
            return {
                "completed_dataset_id": "dataset-1",
                "completed_dataset_revision": "artifact-1.v1",
                "session_id": "other-session",
                "task_id": "rollout-attempt-1",
            }

    with pytest.raises(CoreControlError, match="does not match the sealed candidate"):
        CoreFeedbackPort._completed_dataset_authority(
            Client(),
            {
                "dataset_id": "dataset-1",
                "dataset_revision": "artifact-1.v1",
                "session_id": "session-1",
            },
        )


def test_composite_binds_native_successor_head_and_rejects_silent_rollback(
    tmp_path: Path,
) -> None:
    composites = LocalCompositePort(tmp_path)
    composites.register_baseline(
        project_id="project-1",
        project_head={
            "project_head_id": "project-head-genesis",
            "manifest_sha256": "e" * 64,
            "evolution_revision": {"artifact_count": 0},
        },
    )
    jobs = [
        {
            "job_id": f"job-{artifact_type}",
            "artifact_type": artifact_type,
            "action": "update",
            "successor_registry_id": f"registry-{artifact_type}",
            "successor_sha256": str(index + 1) * 64,
            "promotion_result": "promoted",
            "admission_result": "accepted",
            "proposal_ids": [f"registry-{artifact_type}"],
            "admission_decision_id": f"decision-{artifact_type}",
            "admission_decision_sha256": str(index + 4) * 64,
            "content_admission": _content_admission(
                f"registry-{artifact_type}"
            ),
        }
        for index, artifact_type in enumerate(("agent_system", "text_memory", "skill_bundle"))
    ]
    for job in jobs:
        job["content_admission_sha256"] = job["content_admission"][
            "content_sha256"
        ]
    composite = composites.execute(
        {
            "parent_composite_id": "c000",
            "evolution": {
                "jobs": jobs,
                "successor_project_head": {
                    "project_head_id": "project-head-selected",
                    "manifest_sha256": "f" * 64,
                },
            },
        },
        "composite-operation-1",
    )
    assert composite["core_project_head_id"] == "project-head-selected"
    assert set(composite["admission_receipts"]) == set(ARTIFACT_TYPES)
    _require_selected_core_head(
        composites,
        selected_composite_id=composite["composite_id"],
        active_project_head={"project_head_id": "project-head-selected"},
    )
    with pytest.raises(ValueError, match="native historical restore"):
        _require_selected_core_head(
            composites,
            selected_composite_id=composite["composite_id"],
            active_project_head={"project_head_id": "project-head-latest-regressed"},
        )


def test_cross_task_sanitizer_requires_nonempty_core_content_admission() -> None:
    registry_artifacts = {}
    admission_receipts = {}
    for index, artifact_type in enumerate(ARTIFACT_TYPES):
        registry_id = f"registry-{artifact_type}"
        content = _content_admission(registry_id)
        registry_artifacts[artifact_type] = {
            "registry_id": registry_id,
            "sha256": str(index + 1) * 64,
            "action": "update",
        }
        admission_receipts[artifact_type] = {
            "job_id": f"job-{artifact_type}",
            "action": "update",
            "proposal_ids": [registry_id],
            "admission_decision_id": f"decision-{artifact_type}",
            "admission_decision_sha256": str(index + 4) * 64,
            "content_admission": content,
            "content_admission_sha256": content["content_sha256"],
            "promotion_result": "promoted",
            "admission_result": "accepted",
        }
    composite = {
        "composite_id": "composite-verified",
        "registry_artifacts": registry_artifacts,
        "registry_artifact_ids": [
            registry_artifacts[item]["registry_id"] for item in ARTIFACT_TYPES
        ],
        "admission_receipts": admission_receipts,
    }
    verified = _cross_task_content_admission_authority(composite)
    assert verified["artifact_count"] == 3
    assert verified["passed"] is True
    composite["admission_receipts"] = {}
    with pytest.raises(ValueError, match="exact admission inventory"):
        _cross_task_content_admission_authority(composite)


def test_invalid_validator_receipt_still_freezes_candidate_outputs(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "candidate"
    for relative in ("code", "outputs", "report"):
        (workspace / relative).mkdir(parents=True, exist_ok=True)
    report = workspace / "report" / "report.md"
    report.write_text("incomplete", encoding="utf-8")
    receipt = ports_module.LocalValidationPort().execute(
        {"candidate": {"candidate_output_root": str(workspace)}},
        "validator-invalid",
    )
    assert receipt["artifact_valid"] is False
    assert receipt["outputs_frozen_read_only"] is True
    assert report.stat().st_mode & 0o222 == 0


def test_cross_task_sanitizer_abandons_final_transition_and_restores_selected_head(
    tmp_path: Path,
    monkeypatch,
) -> None:
    composites = LocalCompositePort(tmp_path)
    composites.register_baseline(
        project_id="project-1",
        project_head={
            "project_head_id": "head-selected",
            "manifest_sha256": "1" * 64,
            "evolution_revision": {"artifact_count": 0},
        },
    )

    class Client:
        cancelled = False

        def __init__(self, *_args, **_kwargs):
            pass

        def close(self):
            pass

        def json(self, method, path, *, payload=None, headers=None, content=None):
            if path == "/v2/internal/training-successors/transition-a2":
                if self.cancelled:
                    raise AssertionError(
                        "sanitizer must not poll the closed task after abandon"
                    )
                return {
                    "transition": {
                        "state": "failed",
                        "transition": {
                            "predecessor_project_head": {
                                "project_head_id": "head-before-a2"
                            }
                        },
                    }
                }
            if path == "/v2/transitions/transition-a2/abandon":
                assert method == "POST"
                assert headers and headers["Idempotency-Key"].endswith(":abandon")
                self.cancelled = True
                return {
                    "operation_id": "abandon-a2",
                    "kind": "transition_abandon",
                    "status": "succeeded",
                }
            if path == "/v2/projects/project-1":
                return {
                    "active_project_head": {
                        "project_head_id": "head-after-abandon",
                        "manifest_sha256": "2" * 64,
                    }
                }
            if path == "/v2/internal/projects/project-1/historical-restores":
                assert payload["source_project_head_id"] == "head-selected"
                return {
                    "restore_request_id": "historical-restore-1",
                    "successor_project_head": {
                        "project_head_id": "head-restored",
                        "manifest_sha256": "3" * 64,
                    },
                }
            raise AssertionError((method, path))

    monkeypatch.setattr(ports_module, "CoreControlV2Client", Client)
    sanitizer = LocalSanitizerPort(
        composites,
        config=object(),
        root=tmp_path,
        core_authority=object(),
    )
    result = sanitizer.execute(
        {
            "task_id": CANARY_TASK,
            "composite_id": "c000",
            "core_project_id": "project-1",
            "final_candidate": {
                "successor_transition_id": "transition-a2"
            },
        },
        "sanitize-best-of-three",
    )
    assert result["passed"] is True
    assert result["source_composite_id"] == "c000"
    assert result["restored_project_head_id"] == "head-restored"
    restored = composites.get_composite(result["composite_id"])
    assert restored["historical_restore_source_composite_id"] == "c000"
    assert restored["registry_artifact_ids"] == []


def test_cross_task_sanitizer_recovery_defers_to_idempotent_execute_without_journal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    composites = LocalCompositePort(tmp_path)
    composites.register_baseline(
        project_id="project-1",
        project_head={
            "project_head_id": "head-selected",
            "manifest_sha256": "1" * 64,
            "evolution_revision": {"artifact_count": 0},
        },
    )

    class UnexpectedClient:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("recover must not issue a non-authoritative GET probe")

    monkeypatch.setattr(ports_module, "CoreControlV2Client", UnexpectedClient)
    sanitizer = LocalSanitizerPort(
        composites,
        config=object(),
        root=tmp_path,
        core_authority=object(),
    )
    request = {
        "task_id": CANARY_TASK,
        "composite_id": "c000",
        "core_project_id": "project-1",
        "final_candidate": {"successor_transition_id": "transition-a2"},
    }

    assert sanitizer.recover(request, "sanitize-best-of-three") is None


def test_cross_task_sanitizer_forks_selected_authority_into_next_workspace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    composites = LocalCompositePort(tmp_path)
    composites.register_baseline(
        project_id="project-source",
        project_head={
            "project_head_id": "head-selected",
            "manifest_sha256": "1" * 64,
            "evolution_revision": {"artifact_count": 0},
        },
    )

    class Client:
        cancelled = False

        def __init__(self, *_args, **_kwargs):
            pass

        def close(self):
            pass

        def json(self, method, path, *, payload=None, headers=None, content=None):
            if path == "/v2/internal/training-successors/transition-a2":
                if self.cancelled:
                    raise AssertionError(
                        "sanitizer must not poll the closed task after abandon"
                    )
                return {
                    "transition": {
                        "state": "failed",
                        "transition": {
                            "predecessor_project_head": {
                                "project_head_id": "head-before-a2"
                            }
                        },
                    }
                }
            if path == "/v2/transitions/transition-a2/abandon":
                self.cancelled = True
                return {
                    "operation_id": "abandon-a2",
                    "kind": "transition_abandon",
                    "status": "succeeded",
                }
            if path == "/v2/projects/project-source":
                return {
                    "active_project_head": {
                        "project_head_id": "head-after-abandon",
                        "manifest_sha256": "2" * 64,
                    }
                }
            if path == "/v2/internal/projects/project-source/historical-restores":
                return {
                    "restore_request_id": "historical-restore-source",
                    "successor_project_head": {
                        "project_head_id": "head-source-restored",
                        "project_id": "project-source",
                        "manifest_sha256": "3" * 64,
                    },
                }
            if path == "/v2/internal/projects/project-destination/historical-restores":
                assert method == "POST"
                assert payload["mode"] == "cross_project_fork"
                assert payload["source_project_id"] == "project-source"
                assert payload["source_project_head_id"] == "head-source-restored"
                return {
                    "restore_request_id": "historical-restore-destination",
                    "successor_project_head": {
                        "project_head_id": "head-destination-seeded",
                        "project_id": "project-destination",
                        "manifest_sha256": "5" * 64,
                    },
                }
            raise AssertionError((method, path))

    monkeypatch.setattr(ports_module, "CoreControlV2Client", Client)
    sanitizer = LocalSanitizerPort(
        composites,
        config=object(),
        root=tmp_path,
        core_authority=object(),
    )
    monkeypatch.setattr(
        sanitizer,
        "_prepare_cross_task_destination",
        lambda **_kwargs: {
            "project_id": "project-destination",
            "project_head": {
                "project_head_id": "head-destination-genesis",
                "project_id": "project-destination",
                "generation": 0,
                "manifest_sha256": "4" * 64,
            },
            "workspace_run_id": "Chemistry_004_a0_cross_task_test",
        },
    )
    result = sanitizer.execute(
        {
            "task_id": CANARY_TASK,
            "next_task_id": "Chemistry_004",
            "composite_id": "c000",
            "core_project_id": "project-source",
            "final_candidate": {"successor_transition_id": "transition-a2"},
        },
        "sanitize-cross-project",
    )
    assert result["passed"] is True
    assert result["cross_project_forked"] is True
    assert result["core_project_id"] == "project-destination"
    assert result["restored_project_head_id"] == "head-destination-seeded"
    restored = composites.get_composite(result["composite_id"])
    assert restored["core_project_id"] == "project-destination"
    assert restored["restore_mode"] == "cross_project_fork"


def test_per_item_evolved_workspace_rebinds_only_same_task_artifacts_to_clean_head(
    tmp_path: Path,
    monkeypatch,
) -> None:
    selected = {
        "composite_id": "composite-life-evolved",
        "composite_sha256": "1" * 64,
        "core_project_id": "project-life-baseline",
        "core_project_head_id": "head-life-successor",
        "core_project_head_manifest_sha256": "2" * 64,
        "registry_artifact_ids": ["art-system", "art-memory", "art-skill"],
        "artifact_hashes": {
            "agent_system": "3" * 64,
            "text_memory": "4" * 64,
            "skill_bundle": "5" * 64,
        },
        "registry_artifacts": {
            "agent_system": {"registry_id": "art-system"},
            "text_memory": {"registry_id": "art-memory"},
            "skill_bundle": {"registry_id": "art-skill"},
        },
        "task_local_overlay_id": None,
    }

    class Composites:
        def get_composite(self, composite_id):
            assert composite_id == selected["composite_id"]
            return selected

        def register_restored(self, **kwargs):
            assert kwargs["selected_composite_id"] == selected["composite_id"]
            assert kwargs["restore_mode"] == "per_item_same_task_clean_workspace"
            assert kwargs["project_id"] == "project-life-evolved"
            return {
                **selected,
                "composite_id": "composite-life-evolved-clean",
                "core_project_id": "project-life-evolved",
                "core_project_head_id": kwargs["project_head"]["project_head_id"],
            }

    monkeypatch.setattr(
        ports_module,
        "_cross_task_content_admission_authority",
        lambda _selected: {"passed": True},
    )

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def close(self):
            pass

        def json(self, method, path, *, payload=None, **_kwargs):
            if path == "/v2/projects/project-life-baseline":
                assert method == "GET"
                return {
                    "active_project_head": {
                        "project_head_id": "head-life-successor",
                        "manifest_sha256": "2" * 64,
                        "evolution_revision": {"artifact_count": 3},
                    }
                }
            if path == "/v2/internal/projects/project-life-evolved/historical-restores":
                assert method == "POST"
                assert payload["mode"] == "cross_project_fork"
                assert payload["source_project_id"] == "project-life-baseline"
                return {
                    "restore_request_id": "restore-life-same-task",
                    "successor_project_head": {
                        "project_head_id": "head-life-evolved",
                        "project_id": "project-life-evolved",
                        "generation": 1,
                        "predecessor_project_head_id": "head-life-genesis",
                        "manifest_sha256": "6" * 64,
                        "workspace_snapshot": {
                            "workspace_snapshot_id": "workspace-life-clean",
                            "manifest_sha256": "7" * 64,
                        },
                        "evolution_revision": {"artifact_count": 3},
                    },
                }
            raise AssertionError((method, path))

    monkeypatch.setattr(ports_module, "CoreControlV2Client", Client)
    port = PerItemEvolvedWorkspacePort(
        Composites(),
        config=object(),
        root=tmp_path,
        core_authority=object(),
    )
    archive = WorkspaceArchiveDeclarationV2(
        format="openevo_deterministic_tar_v1",
        media_type="application/vnd.openevo.workspace-tar",
        content_sha256="8" * 64,
        byte_size=1024,
        entry_count=0,
        extracted_byte_size=0,
    )
    monkeypatch.setattr(
        port.destination_candidate,
        "prepare_preseeded_destination",
        lambda *_args, **_kwargs: {
            "project_id": "project-life-evolved",
            "project_head": {
                "project_head_id": "head-life-genesis",
                "project_id": "project-life-evolved",
                "generation": 0,
                "predecessor_project_head_id": None,
                "manifest_sha256": "9" * 64,
                "workspace_snapshot": {
                    "workspace_snapshot_id": "workspace-life-clean",
                    "manifest_sha256": "7" * 64,
                },
                "evolution_revision": {"artifact_count": 0},
            },
            "workspace_archive": archive.model_dump(mode="json"),
        },
    )
    result = port.execute(
        {
            "task_id": "Life_005",
            "attempt_index": 0,
            "source_core_project_id": "project-life-baseline",
            "admitted_composite": {
                "composite_id": selected["composite_id"],
                "registry_artifact_ids": selected["registry_artifact_ids"],
            },
            "ground_truth_sha256": "a" * 64,
            "judge_feedback": None,
            "fresh_workspace": True,
            "same_task_only": True,
        },
        "per-item-life-evolved",
    )
    assert result["same_task_only"] is True
    assert result["fresh_generation_zero_destination"] is True
    assert result["cross_task_inheritance"] is False
    assert result["raw_gt_carried"] is False
    assert result["judge_feedback_carried"] is False
    assert result["recovery_command_used"] is False
    assert result["recovery_path_used"] is False
    assert result["artifact_ids"] == selected["registry_artifact_ids"]
    assert result["preseeded_workspace_authority"]["archive_content_sha256"] == (
        archive.content_sha256
    )


def test_per_item_production_ports_use_native_candidate_and_successor_adapters(
    tmp_path: Path,
) -> None:
    source = ExperimentConfig.load(
        Path(__file__).resolve().parents[3]
        / "configs/researchclawbench/per_item_community17.yaml"
    )
    raw = json.loads(json.dumps(source.raw))
    raw["paths"]["project_root"] = "/"
    raw["paths"]["experiment_root"] = str(tmp_path)
    config = ExperimentConfig(source.path, raw)

    ports = build_production_ports(
        config,
        tmp_path / "production-run",
        core_authority=_core_authority(),
    )

    assert isinstance(ports.candidate, CoreV2CandidatePort)
    assert isinstance(ports.evolution, CoreSuccessorPort)
    assert isinstance(ports.per_item_evolved, PerItemEvolvedWorkspacePort)


def test_per_item_supervisor_closes_through_production_operation_driver(
    tmp_path: Path,
) -> None:
    source = ExperimentConfig.load(
        Path(__file__).resolve().parents[3]
        / "configs/researchclawbench/per_item_community17.yaml"
    )
    raw = json.loads(json.dumps(source.raw))
    raw["reflector"]["require_credential_mount_readiness"] = False
    config = ExperimentConfig(source.path, raw)
    ports, authorities = build_synthetic_ports(tmp_path / "external")
    run_id = "rcb_oe_v0_per_item_production_contract"
    operations = ProductionTrainingOperations(
        config=config,
        experiment_run_id=run_id,
        receipt_root=tmp_path / "operations",
        ports=ports,
    )
    supervisor = CommunityTrainingSupervisor(
        store=TrainingStateStore(tmp_path / "supervisor"),
        experiment_id=run_id,
        identity=_identity(config),
        operations=operations,
        task_ids=("Life_005",),
        current_task_gt_supervision=True,
        per_item_reset=True,
    )
    supervisor.initialize()
    for _ in range(100):
        state = supervisor.status()
        if state["stage"] == "ITEM_RESET":
            break
        supervisor.run_next()
    else:  # pragma: no cover - bounded fail-closed guard
        raise AssertionError("per-item production driver did not close")

    state = supervisor.status()
    assert state["stage"] == "ITEM_RESET"
    assert state["paired_result"]["artifact_consumption"][
        "artifact_read_requested"
    ] is True
    assert authorities["candidate"].call_count() == 2
    assert authorities["evaluation"].call_count() == 2
    assert authorities["attachment"].call_count() == 1
    assert authorities["evolution"].call_count() == 1
    assert authorities["per_item_evolved"].call_count() == 1


def test_full_synthetic_single_task_three_attempt_pipeline(tmp_path: Path, monkeypatch) -> None:
    _config, supervisor, authorities = _build(tmp_path, monkeypatch)
    supervisor.initialize()
    final = _drive(supervisor)
    assert final["stage"] == TrainingStage.FINAL_FROZEN.value
    assert len(final["session_ids"]) == 3
    assert len(final["dataset_ids"]) == 3
    assert len(final["attachment_ids"]) == 2
    assert len(final["evolution_job_ids"]) == 6
    assert len(final["composite_ids"]) == 3
    assert final["task_best"][CANARY_TASK]["attempt_index"] == 1
    assert final["current_task_local_overlay_id"] is None
    assert authorities["candidate"].call_count() == 3
    assert authorities["evaluation"].call_count() == 3
    assert authorities["attachment"].call_count() == 2
    assert authorities["evolution"].call_count() == 2
    assert authorities["composite"].call_count() == 2
    assert authorities["task_local"].call_count() == 1
    assert authorities["sanitizer"].call_count() == 1
    assert authorities["freeze"].call_count() == 1
    assert supervisor.store.active_resources(supervisor.experiment_id) == []


def test_full_synthetic_community17_pipeline_has_closed_51_34_102_inventory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("JUDGE_API_KEY", "synthetic-present")
    monkeypatch.setenv("JUDGE_API_BASE", "https://synthetic.invalid")
    monkeypatch.setenv("JUDGE_MODEL_NAME", "synthetic-no-model")
    config = ExperimentConfig.load(PROTOCOL)
    ports, authorities = build_synthetic_ports(tmp_path / "full-external")
    run_id = "rcb_oe_v0_synthetic_community17_v11"
    operations = ProductionTrainingOperations(
        config=config,
        experiment_run_id=run_id,
        receipt_root=tmp_path / "full-operations",
        ports=ports,
    )
    supervisor = CommunityTrainingSupervisor(
        store=TrainingStateStore(tmp_path / "full-supervisor"),
        experiment_id=run_id,
        identity=_identity(config),
        operations=operations,
        task_ids=FROZEN_TASKS,
    )
    supervisor.initialize()
    final = _drive(supervisor, limit=5_000)
    assert final["stage"] == TrainingStage.FINAL_FROZEN.value
    assert len(final["session_ids"]) == 51
    assert len(set(final["session_ids"])) == 51
    assert len(final["dataset_ids"]) == 51
    assert len(set(final["dataset_ids"])) == 51
    assert len(final["attachment_ids"]) == 34
    assert len(final["evolution_job_ids"]) == 102
    assert len(set(final["evolution_job_ids"])) == 102
    assert set(final["task_best"]) == set(FROZEN_TASKS)
    assert authorities["candidate"].call_count() == 51
    assert authorities["evaluation"].call_count() == 51
    assert authorities["attachment"].call_count() == 34
    assert authorities["evolution"].call_count() == 34
    assert authorities["composite"].call_count() == 34
    assert authorities["task_local"].call_count() == 17
    assert authorities["sanitizer"].call_count() == 17
    assert authorities["freeze"].call_count() == 1
    verification = supervisor.verify()
    assert verification["status"] == "PASS"
    assert verification["attempt_receipts"] == 51
    assert verification["completed_reflector_cycles"] == 34
    assert verification["evolution_job_ids"] == 102
    assert verification["task_best_count"] == 17
    assert verification["pending_side_effects"] == 0
    assert verification["active_owned_resources"] == []


def test_community17_final_freeze_is_directly_accepted_by_official40_plan(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Exercise the real Community-final-state to Official-plan contract.

    The Community and Official state machines have independent integration
    suites.  This test deliberately crosses their boundary so a renamed freeze
    authority or disabled-policy field cannot be hidden by a hand-written
    Official fixture.
    """

    monkeypatch.setenv("JUDGE_API_KEY", "synthetic-present")
    monkeypatch.setenv("JUDGE_API_BASE", "https://synthetic.invalid")
    monkeypatch.setenv("JUDGE_MODEL_NAME", "synthetic-no-model")
    config = ExperimentConfig.load(PROTOCOL)
    ports, _authorities = build_synthetic_ports(tmp_path / "handoff-external")
    identity = _identity(config)
    run_id = "rcb_oe_v0_synthetic_community17_official_handoff_v11"
    operations = ProductionTrainingOperations(
        config=config,
        experiment_run_id=run_id,
        receipt_root=tmp_path / "handoff-operations",
        ports=ports,
    )
    supervisor = CommunityTrainingSupervisor(
        store=TrainingStateStore(tmp_path / "handoff-supervisor"),
        experiment_id=run_id,
        identity=identity,
        operations=operations,
        task_ids=FROZEN_TASKS,
    )
    supervisor.initialize()
    final = _drive(supervisor, limit=5_000)

    task_ids = tuple(f"Official_{index:03d}" for index in range(40))
    task_root = tmp_path / "official-tasks"
    task_root.mkdir()
    for task_id in task_ids:
        (task_root / task_id).mkdir()
    split = tmp_path / "official-split.yaml"
    split.write_text(
        yaml.safe_dump({"official_test_tasks": list(task_ids)}, sort_keys=False),
        encoding="utf-8",
    )
    plan = load_official_frozen_plan(
        split_config=split,
        expected_split_sha256=hashlib.sha256(split.read_bytes()).hexdigest(),
        official_task_root=task_root,
        final_state=final,
        identity=OfficialTrainingIdentity(
            protocol_sha256=identity.protocol_sha256,
            core_identity_sha256=identity.core_identity_sha256,
            adapter_identity_sha256=identity.adapter_identity_sha256,
        ),
    )
    assert plan.task_ids == task_ids
    assert plan.freeze_authority_sha256 == final["final_freeze_receipt"][
        "authority_sha256"
    ]
    assert plan.frozen_composite_id == final["final_freeze_receipt"]["composite_id"]
    assert set(plan.frozen_registry_artifacts) == set(ARTIFACT_TYPES)


def test_core_freeze_projection_exposes_official_plan_authority_contract() -> None:
    authority = {
        "freeze_receipt_id": "freeze-core-1",
        "authority_sha256": "a" * 64,
        "core_authoritative": True,
        "frozen": True,
        "protocol_sha256": "b" * 64,
        "core_identity_sha256": "c" * 64,
        "adapter_identity_sha256": "d" * 64,
        "project_id": "project-core-1",
        "project_head_id": "head-core-1",
        "project_head_manifest_sha256": "e" * 64,
        "composite_id": "composite-core-1",
        "composite_sha256": "f" * 64,
        "artifacts": [
            {
                "artifact_type": artifact_type,
                "registry_id": f"registry-{artifact_type}",
                "sha256": hashlib.sha256(artifact_type.encode()).hexdigest(),
            }
            for artifact_type in ARTIFACT_TYPES
        ],
    }
    projected = CoreProjectFreezePort._result(authority)
    assert projected["authority_sha256"] == authority["authority_sha256"]
    assert projected["freeze_authority_sha256"] == authority["authority_sha256"]
    assert projected["teacher_enabled"] is False
    assert projected["hard_gt_teacher_enabled"] is False


CRASH_MATRIX = {
    "workspace_created": "candidate",
    "session_result_created": "candidate",
    "completed_dataset_sealed": "candidate",
    "validator_completed": "validation",
    "judge_completed": "evaluation",
    "attachment_created": "attachment",
    "attachment_resolved": "attachment",
    "successor_submitted": "evolution",
    "registry_revision_generated": "evolution",
    "composite_written": "composite",
    "best_selection_committed": "task_local",
    "sanitizer_completed": "sanitizer",
}


@pytest.mark.parametrize(("crash_label", "port_kind"), CRASH_MATRIX.items())
def test_crash_recovery_matrix_has_no_duplicate_external_side_effect(
    tmp_path: Path,
    monkeypatch,
    crash_label: str,
    port_kind: str,
) -> None:
    _config, supervisor, authorities = _build(tmp_path, monkeypatch, crash_point=port_kind)
    supervisor.initialize()
    with pytest.raises(InjectedCrash, match=port_kind):
        _drive(supervisor)
    counts_before = {kind: authority.call_count() for kind, authority in authorities.items()}

    # A fresh process would reconstruct both the durable supervisor and the
    # production driver.  The fixture does exactly that over the same stores.
    _config, recovered, recovered_authorities = _build(tmp_path, monkeypatch)
    final = _drive(recovered)
    assert final["stage"] == TrainingStage.FINAL_FROZEN.value
    expected = {
        "candidate": 3,
        "validation": 3,
        "evaluation": 3,
        "attachment": 2,
        "evolution": 2,
        "composite": 2,
        "task_local": 1,
        "sanitizer": 1,
        "freeze": 1,
    }
    assert {kind: item.call_count() for kind, item in recovered_authorities.items()} == expected
    assert all(counts_before[kind] <= expected[kind] for kind in expected)
    receipts = list((tmp_path / "operations").glob("*.json"))
    assert receipts
    for path in receipts:
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["content_sha256"]
