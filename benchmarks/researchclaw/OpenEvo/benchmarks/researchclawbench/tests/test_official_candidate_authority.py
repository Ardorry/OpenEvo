from __future__ import annotations

import json
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import openevo_researchclawbench.official_candidate_authority as module
import pytest
from openevo.runtime.codex_isolation import (
    CODEX_SUBSCRIPTION_POLICY_ID,
    CODEX_SUBSCRIPTION_POLICY_SHA256,
)
from openevo_researchclawbench.config import ARTIFACT_TYPES, ExperimentConfig
from openevo_researchclawbench.managed_core_control import ManagedCoreControlAuthority
from openevo_researchclawbench.official_candidate_authority import (
    ProductionFrozenOfficialCandidateCoreAuthority,
    _official_project_config,
)
from openevo_researchclawbench.official_training_operations import (
    OFFICIAL_DISABLED_POLICY,
)
from openevo_researchclawbench.production_operation_ports import CoreControlError
from openevo_researchclawbench.workspace import WorkspaceReceipt
from pydantic import SecretStr


def _config(tmp_path: Path) -> ExperimentConfig:
    project_root = tmp_path / "project"
    experiment_root = project_root / "experiment"
    benchmark_root = project_root / "ResearchClawBench"
    for path in (experiment_root, benchmark_root):
        path.mkdir(parents=True)
    protocol = project_root / "protocol.yaml"
    protocol.write_text("test: true\n", encoding="utf-8")
    return ExperimentConfig(
        protocol,
        {
            "paths": {
                "project_root": str(project_root),
                "experiment_root": str(experiment_root),
                "researchclawbench_root": str(benchmark_root),
            },
            "candidate": {
                "model": "gpt-test",
                "reasoning_level": "high",
                "token_limit": 4096,
                "attempt_timeout_seconds": 60,
            },
        },
    )


def _core_authority() -> ManagedCoreControlAuthority:
    return ManagedCoreControlAuthority(
        base_url="http://127.0.0.1:17810",
        service_identity_id="core-service-official-test",
        generation="1" * 32,
        release_identity="2" * 64,
        registry_digest="3" * 64,
        source_commit="4" * 40,
        status_proof="5" * 64,
        attached=True,
        managed_host_profile_ready=True,
        _bearer=SecretStr("secret-not-serialized"),
    )


def _artifacts() -> dict[str, dict[str, str]]:
    return {
        "agent_system": {"registry_id": "as-frozen", "sha256": "a" * 64},
        "text_memory": {"registry_id": "tm-frozen", "sha256": "b" * 64},
        "skill_bundle": {"registry_id": "sk-frozen", "sha256": "c" * 64},
    }


def _request() -> dict[str, Any]:
    return {
        "experiment_id": "rcb-official-test",
        "task_id": "Astronomy_001",
        "task_index": 0,
        "attempt_index": 0,
        "run_id": "Astronomy_001_a0_official_test",
        "fresh_workspace": True,
        "resume_in_place": False,
        "frozen_protocol_sha256": "6" * 64,
        "freeze_receipt_sha256": "7" * 64,
        "freeze_receipt_id": "freeze-receipt",
        "freeze_authority_sha256": "8" * 64,
        "frozen_composite_id": "composite-frozen",
        "frozen_composite_sha256": "9" * 64,
        "frozen_registry_artifacts": _artifacts(),
        "core_project_id": "project-community-frozen",
        "core_project_head_id": "head-community-frozen",
        "core_project_head_manifest_sha256": "d" * 64,
        "official_policy": dict(OFFICIAL_DISABLED_POLICY),
    }


def _official_task_ids() -> tuple[str, ...]:
    return tuple(f"Astronomy_{index:03d}" for index in range(1, 41))


def _workspace(run_root: Path, request: dict[str, Any]) -> WorkspaceReceipt:
    workspace = run_root / "runs" / request["run_id"]
    candidate = run_root / "sanitized_workspaces" / request["task_id"]
    for root in (workspace, candidate):
        for relative in ("data", "related_work", "code", "outputs", "report"):
            (root / relative).mkdir(parents=True, exist_ok=True)
        (root / "INSTRUCTIONS.md").write_text("Do the task.\n", encoding="utf-8")
    (workspace / "_meta.json").write_text(
        json.dumps({"status": "pending", "exit_code": None}), encoding="utf-8"
    )
    return WorkspaceReceipt(
        task_id=request["task_id"],
        run_id=request["run_id"],
        workspace=workspace,
        candidate_workspace=candidate,
        instructions=workspace / "INSTRUCTIONS.md",
        data=workspace / "data",
        related_work=workspace / "related_work",
        code=workspace / "code",
        outputs=workspace / "outputs",
        report=workspace / "report",
    )


class _FakeClient:
    def __init__(
        self,
        config: ExperimentConfig,
        *,
        lose_task_response: bool,
        public_generation: str = "1" * 32,
        service_generation: str = "a" * 64,
        receipt_generation: str | None = None,
    ) -> None:
        self.config = config
        self.lose_task_response = lose_task_response
        self.public_generation = public_generation
        self.service_generation = service_generation
        self.receipt_generation = receipt_generation or service_generation
        self.project_config: dict[str, Any] | None = None
        self.destination_project_id = "project-official-destination"
        self.project_config_sha256 = "e" * 64
        self.genesis = {
            "project_id": self.destination_project_id,
            "project_head_id": "head-official-genesis",
            "manifest_sha256": "f" * 64,
            "generation": 0,
        }
        self.seeded = {
            "project_id": self.destination_project_id,
            "project_head_id": "head-official-seeded",
            "manifest_sha256": "0" * 64,
            "generation": 1,
            "predecessor_project_head_id": self.genesis["project_head_id"],
        }
        self.active: dict[str, Any] | None = None
        self.task: dict[str, Any] | None = None
        self.task_posts = 0
        self.closed = 0

    def close(self) -> None:
        self.closed += 1

    def readiness(self) -> dict[str, Any]:
        return {
            "service_reachable": True,
            "bearer_present": True,
            "bearer_valid": True,
            "generation_matches": True,
            "candidate_port_authorized": True,
            "secret_recorded": False,
            "generation": self.public_generation,
            "release_identity": "2" * 64,
        }

    def _project(self) -> dict[str, Any]:
        return {
            "project_id": self.destination_project_id,
            "display_name": "official",
            "config": self.project_config,
            "project_config_sha256": self.project_config_sha256,
            "active_project_head": self.active,
            "state": "ready" if self.active is not None else "not_ready",
            "etag": '"' + "1" * 64 + '"',
            "admission_etag": '"' + "2" * 64 + '"',
        }

    def json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        if path.startswith("/v2/capabilities"):
            return {"registry_digest": "3" * 64}
        if path == "/v2/internal/managed-candidate/readiness":
            return {
                "core_generation": self.service_generation,
                "daemon_release_identity": "2" * 64,
                "release_registry_digest": "3" * 64,
                "candidate_runtime_ready": True,
                "credential_mount_adopted": True,
                "codex_cli_started": True,
                "model_started": False,
                "secret_recorded": False,
                "managed_candidate_runtime": {
                    "generation_digest": self.receipt_generation,
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
                    "isolation_policy_id": CODEX_SUBSCRIPTION_POLICY_ID,
                    "isolation_policy_sha256": (
                        CODEX_SUBSCRIPTION_POLICY_SHA256
                    ),
                    "authority_issued": True,
                },
            }
        if method == "POST" and path == "/v2/projects":
            assert payload is not None
            if self.project_config is None:
                self.project_config = payload["config"]
            assert self.project_config == payload["config"]
            return self._project()
        if path.endswith("/heads?limit=100&direction=asc"):
            return {"items": [] if self.active is None else [self.genesis]}
        if path == "/v2/project-heads/head-official-genesis":
            return self.genesis
        if method == "POST" and path.endswith("/workspace-uploads"):
            return {"upload_id": "upload-1", "etag": '"' + "3" * 64 + '"'}
        if method == "PUT" and "/chunks/" in path:
            return {"upload_id": "upload-1", "etag": '"' + "4" * 64 + '"'}
        if method == "POST" and path.endswith("/finalize"):
            self.active = self.genesis
            return {"upload_id": "upload-1", "etag": '"' + "5" * 64 + '"'}
        if method == "GET" and path == "/v2/projects/project-official-destination":
            return self._project()
        if method == "GET" and path.startswith(
            "/v2/internal/frozen-project-forks/"
        ):
            if self.active != self.seeded:
                raise CoreControlError("not found", status_code=404)
            return {"fake": "fork"}
        if method == "POST" and path == "/v2/internal/frozen-project-forks":
            self.active = self.seeded
            return {"fake": "fork"}
        if method == "POST" and path == "/v2/tasks":
            self.task_posts += 1
            assert payload is not None
            self.task = {
                "task_id": "task-official-1",
                "project_id": self.destination_project_id,
                "admission": {"predecessor_project_head": self.seeded},
                "attempts": [{"attempt_id": "attempt-official-1"}],
                "successor_transition": None,
            }
            if self.lose_task_response:
                self.lose_task_response = False
                raise CoreControlError("response lost", status_code=503)
            return self.task
        if method == "GET" and path.startswith("/v2/tasks?limit=100"):
            return {"items": [] if self.task is None else [self.task]}
        if method == "GET" and path == "/v2/tasks/task-official-1":
            assert self.task is not None
            return self.task
        if method == "GET" and path.endswith("/execution-status"):
            return {
                "task_id": "task-official-1",
                "attempt_id": "attempt-official-1",
                "state": "captured",
                "terminal": True,
                "captured": True,
                "error_code": None,
                "failure_authority": None,
                "model_started": True,
                "benchmark_started": True,
            }
        if method == "GET" and path.startswith(
            "/v2/internal/training-attempts/task-official-1/attempt-official-1"
        ):
            return {
                "session_result": {
                    "trajectory": {"traces": []},
                },
                "execution_receipt": {
                    "workspace_handoff_id": "handoff-1",
                    "rollout_payload_sha256": "6" * 64,
                    "session_result_sha256": "7" * 64,
                    "session_id": "session-official-1",
                },
            }
        if method == "GET" and path.endswith("/timeline?limit=100"):
            return {
                "items": [
                    {
                        "event_type": "dataset_sealed",
                        "attempt_id": "attempt-official-1",
                        "dataset_id": "dataset-official-1",
                    }
                ]
            }
        if method == "GET" and path.endswith("/datasets/dataset-official-1"):
            return {
                "completed_dataset_id": "dataset-official-1",
                "completed_dataset_revision": "dataset-official-1.v1",
                "session_id": "session-official-1",
            }
        raise AssertionError((method, path, payload))

    def bytes(self, _path: str) -> tuple[bytes, dict[str, str]]:
        return b"sealed-output", {}


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    authority: ProductionFrozenOfficialCandidateCoreAuthority,
    client: _FakeClient,
    run_root: Path,
) -> None:
    monkeypatch.setattr(module, "CoreControlV2Client", lambda _authority: client)
    monkeypatch.setattr(
        module,
        "CapabilitiesResponseV2",
        SimpleNamespace(
            model_validate=lambda _payload: SimpleNamespace(registry_digest="3" * 64)
        ),
    )
    monkeypatch.setattr(
        authority,
        "_source_authority",
        types.MethodType(lambda _self, _client, _request: ({}, {}), authority),
    )
    monkeypatch.setattr(
        module,
        "build_official_workspace",
        lambda *_args, **_kwargs: _workspace(run_root, _request()),
    )
    fake_fork = SimpleNamespace(
        fork_receipt_id=None,
        authority_sha256="8" * 64,
        destination_project_id=client.destination_project_id,
        source_project_id="project-community-frozen",
        source_freeze_receipt_id="freeze-receipt",
        source_freeze_authority_sha256="8" * 64,
        source_mutated=False,
        append_only=True,
        evolution_enabled=False,
        seeded_destination_project_head=SimpleNamespace(
            model_dump=lambda **_kwargs: client.seeded
        ),
    )

    # The real deterministic receipt ID is derived from the POST body.  Capture
    # it directly in a small validating shim instead of constructing the very
    # large Core commit schema in this adapter-focused test.
    class ForkValidator:
        @classmethod
        def model_validate(cls, _payload):
            request = module.FrozenProjectForkRequestV1(
                source_project_id="project-community-frozen",
                source_freeze_receipt_id="freeze-receipt",
                source_freeze_authority_sha256="8" * 64,
                expected_source_project_head_id="head-community-frozen",
                expected_source_project_head_manifest_sha256="d" * 64,
                destination_project_id=client.destination_project_id,
                expected_destination_project_head_id=client.genesis["project_head_id"],
                expected_destination_project_head_manifest_sha256=client.genesis[
                    "manifest_sha256"
                ],
                expected_destination_project_config_sha256=client.project_config_sha256,
                idempotency_key="candidate-key:frozen-fork",
            )
            fake_fork.fork_receipt_id = module._fork_request_id(request)
            return fake_fork

    monkeypatch.setattr(module, "FrozenProjectForkAuthorityV1", ForkValidator)
    monkeypatch.setattr(
        module,
        "_candidate_runtime_injection_authority",
        lambda **_kwargs: {
            "schema_version": "openevo.candidate_runtime_injection.v1",
            "artifact_count": 3,
        },
    )
    fake_session = SimpleNamespace(
        status=module.SessionStatus.COMPLETED,
        workspace_result=SimpleNamespace(
            output_archive=SimpleNamespace(content_sha256="a" * 64)
        ),
        trajectory=SimpleNamespace(traces=[]),
    )
    monkeypatch.setattr(
        module,
        "SessionResult",
        SimpleNamespace(model_validate=lambda _payload: fake_session),
    )
    monkeypatch.setattr(module, "_safe_export_result", lambda *_args: None)


def test_official_project_config_disables_every_evolution_target(tmp_path: Path) -> None:
    project = _official_project_config(_config(tmp_path), "Astronomy_001", "Do it")
    assert set(project.evolution.targets) == {
        *ARTIFACT_TYPES,
        "parametric_memory",
    }
    assert all(not item.enabled and item.method is None for item in project.evolution.targets.values())
    assert project.execution.harness_id == "codex"
    assert project.execution.capture_mode == "transcript"
    assert project.execution.task_network_allow_internet is False


def test_official_preflight_accepts_distinct_typed_core_and_service_generations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    run_root = config.experiment_root / "official-run"
    authority = ProductionFrozenOfficialCandidateCoreAuthority(
        config=config,
        run_root=run_root,
        core_authority=_core_authority(),
        official_task_ids=_official_task_ids(),
    )
    client = _FakeClient(config, lose_task_response=False)
    _install_fakes(monkeypatch, authority, client, run_root)
    result = authority.preflight_frozen_candidate(_request())
    assert result["core_generation"] == "1" * 32
    assert result["managed_candidate_runtime"]["generation_digest"] == "a" * 64
    assert result["core_generation"] != result["managed_candidate_runtime"][
        "generation_digest"
    ]


def test_official_preflight_rejects_service_generation_receipt_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    run_root = config.experiment_root / "official-run"
    authority = ProductionFrozenOfficialCandidateCoreAuthority(
        config=config,
        run_root=run_root,
        core_authority=_core_authority(),
        official_task_ids=_official_task_ids(),
    )
    client = _FakeClient(
        config,
        lose_task_response=False,
        service_generation="a" * 64,
        receipt_generation="b" * 64,
    )
    _install_fakes(monkeypatch, authority, client, run_root)
    with pytest.raises(CoreControlError, match="OFFICIAL_CANDIDATE_RUNTIME_NOT_READY"):
        authority.preflight_frozen_candidate(_request())


def test_response_loss_recovery_queries_existing_task_without_second_post(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    run_root = config.experiment_root / "official-run"
    authority = ProductionFrozenOfficialCandidateCoreAuthority(
        config=config,
        run_root=run_root,
        core_authority=_core_authority(),
        official_task_ids=_official_task_ids(),
    )
    client = _FakeClient(config, lose_task_response=True)
    _install_fakes(monkeypatch, authority, client, run_root)
    request = _request()
    with pytest.raises(CoreControlError, match="response lost"):
        authority.execute_frozen_candidate(request, "candidate-key")
    assert client.task_posts == 1
    result = authority.recover_frozen_candidate(request, "candidate-key")
    assert result is not None
    assert result["session_id"] == "session-official-1"
    assert result["destination_core_project_id"] != request["core_project_id"]
    assert result["evolution_targets_enabled"] == []
    assert result["attachment_ids"] == []
    assert result["evolution_job_ids"] == []
    assert result["frozen_project_fork_source_mutated"] is False
    assert client.task_posts == 1
    assert authority.recover_frozen_candidate(request, "candidate-key") == result
    assert client.task_posts == 1


def test_official_candidate_rejects_training_state_before_core_side_effect(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    authority = ProductionFrozenOfficialCandidateCoreAuthority(
        config=config,
        run_root=config.experiment_root / "official-run",
        core_authority=_core_authority(),
        official_task_ids=_official_task_ids(),
    )
    request = {**_request(), "attachment_ids": ["forbidden"]}
    with pytest.raises(ValueError, match="frozen policy"):
        authority.execute_frozen_candidate(request, "candidate-key")
    assert list(authority.journal.root.iterdir()) == []
