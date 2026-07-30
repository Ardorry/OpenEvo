from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from openevo_researchclawbench.official_training_operations import (
    OFFICIAL_DISABLED_POLICY,
    CoreFrozenOfficialCandidatePort,
    OfficialArtifactValidationPort,
    OfficialUnifiedScorerPort,
    ProductionOfficialTrainingOperations,
    build_official_production_ports,
)
from openevo_researchclawbench.official_training_supervisor import (
    OfficialFrozenRunPlan,
    OfficialTrainingIdentity,
    OfficialTrainingStage,
    OfficialTrainingStateStore,
    OfficialTrainingSupervisor,
)
from openevo_researchclawbench.production_training_operations import (
    ProductionOperationError,
)
from openevo_researchclawbench.training_state_store import canonical_sha256


def _artifacts() -> dict[str, dict[str, str]]:
    return {
        "agent_system": {"registry_id": "as-frozen", "sha256": "6" * 64},
        "text_memory": {"registry_id": "tm-frozen", "sha256": "7" * 64},
        "skill_bundle": {"registry_id": "sk-frozen", "sha256": "8" * 64},
    }


def _plan(tmp_path: Path) -> OfficialFrozenRunPlan:
    identity = OfficialTrainingIdentity(
        protocol_sha256="1" * 64,
        core_identity_sha256="2" * 64,
        adapter_identity_sha256="3" * 64,
    )
    return OfficialFrozenRunPlan(
        task_ids=tuple(f"Domain{index:02d}_{index:03d}" for index in range(40)),
        split_config_path=str(tmp_path / "split.yaml"),
        split_config_sha256="4" * 64,
        source_state_sha256="5" * 64,
        freeze_receipt_sha256="9" * 64,
        freeze_receipt_id="freeze-core-authority",
        freeze_authority_sha256="d" * 64,
        frozen_composite_id="composite-frozen",
        frozen_composite_sha256="a" * 64,
        frozen_registry_artifacts=_artifacts(),
        core_project_id="project-community-final",
        core_project_head_id="head-community-final",
        core_project_head_manifest_sha256="b" * 64,
        identity=identity,
    )


def _candidate_request(plan: OfficialFrozenRunPlan, *, task_index: int = 0) -> dict[str, Any]:
    task_id = plan.task_ids[task_index]
    return {
        "experiment_id": "official-fake-integration",
        "task_id": task_id,
        "task_index": task_index,
        "attempt_index": 0,
        "run_id": f"{task_id}_a0_official_test",
        "fresh_workspace": True,
        "resume_in_place": False,
        "frozen_protocol_sha256": plan.identity.protocol_sha256,
        "freeze_receipt_sha256": plan.freeze_receipt_sha256,
        "freeze_receipt_id": plan.freeze_receipt_id,
        "freeze_authority_sha256": plan.freeze_authority_sha256,
        "frozen_composite_id": plan.frozen_composite_id,
        "frozen_composite_sha256": plan.frozen_composite_sha256,
        "frozen_registry_artifacts": plan.frozen_registry_artifacts,
        "core_project_id": plan.core_project_id,
        "core_project_head_id": plan.core_project_head_id,
        "core_project_head_manifest_sha256": plan.core_project_head_manifest_sha256,
        "official_policy": dict(OFFICIAL_DISABLED_POLICY),
    }


class FakeFrozenCandidateCore:
    """Durable fake of the external Core authority; it never calls a model."""

    def __init__(
        self,
        root: Path,
        plan: OfficialFrozenRunPlan,
        *,
        run_root: Path | None = None,
        failed_candidate_index: int | None = None,
    ) -> None:
        self.root = root
        self.root.mkdir(parents=True)
        self.plan = plan
        self.run_root = run_root or root / "official-run"
        self.failed_candidate_index = failed_candidate_index
        self.execute_count = 0
        self.model_call_count = 0
        self.preflight_overrides: dict[str, Any] = {}
        self.result_overrides: dict[str, Any] = {}
        self.destination_projects: list[str] = []
        self.contexts: list[tuple[str, ...]] = []

    def _path(self, idempotency_key: str) -> Path:
        digest = hashlib.sha256(idempotency_key.encode()).hexdigest()
        return self.root / f"{digest}.json"

    def preflight_frozen_candidate(self, _request: dict[str, Any]) -> dict[str, Any]:
        result = {
            "service_reachable": True,
            "bearer_present": True,
            "bearer_valid": True,
            "generation_matches": True,
            "candidate_port_authorized": True,
            "source_project_head_verified": True,
            "triple_artifact_context_verified": True,
            "independent_project_creation_ready": True,
            "frozen_project_fork_supported": True,
            "all_evolution_targets_disabled": True,
            "core_generation": "generation-official",
            "release_identity": "c" * 64,
            "source_project_id": self.plan.core_project_id,
            "source_freeze_receipt_id": self.plan.freeze_receipt_id,
            "source_freeze_authority_sha256": self.plan.freeze_authority_sha256,
            "source_project_head_id": self.plan.core_project_head_id,
            "source_project_head_manifest_sha256": (
                self.plan.core_project_head_manifest_sha256
            ),
            "context_artifact_ids": [
                artifact["registry_id"]
                for artifact in self.plan.frozen_registry_artifacts.values()
            ],
            "secret_recorded": False,
            "environment_fallback_used": False,
            "model_started": False,
        }
        return {**result, **self.preflight_overrides}

    def recover_frozen_candidate(
        self, request: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any] | None:
        path = self._path(idempotency_key)
        if not path.is_file():
            return None
        receipt = json.loads(path.read_text(encoding="utf-8"))
        if receipt["request_sha256"] != canonical_sha256(request):
            raise ValueError("fake Core recovery request drifted")
        return receipt["result"]

    def execute_frozen_candidate(
        self, request: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]:
        if self._path(idempotency_key).exists():
            raise AssertionError("fake Core candidate executed twice")
        self.execute_count += 1
        index = int(request["task_index"])
        destination = f"project-official-{index:02d}"
        workspace = self.run_root / "runs" / request["run_id"]
        for relative in ("code", "outputs", "report/images"):
            (workspace / relative).mkdir(parents=True, exist_ok=True)
        (workspace / "report/report.md").write_text(
            "Incomplete synthetic report.\n", encoding="utf-8"
        )
        (workspace / "_meta.json").write_text(
            json.dumps({"status": "completed", "exit_code": 0}),
            encoding="utf-8",
        )
        context = tuple(
            artifact["registry_id"]
            for artifact in self.plan.frozen_registry_artifacts.values()
        )
        result = {
            "task_id": request["task_id"],
            "run_id": request["run_id"],
            "attempt_index": 0,
            "session_id": f"session-official-{index:02d}",
            "dataset_id": f"dataset-official-{index:02d}",
            "dataset_revision": f"dataset-official-{index:02d}.v1",
            "completed": index != self.failed_candidate_index,
            "sealed": True,
            "fresh_workspace": True,
            "candidate_output_root": str(workspace),
            "runtime_seconds": 1.0,
            "destination_core_project_id": destination,
            "source_project_id": self.plan.core_project_id,
            "source_freeze_receipt_id": self.plan.freeze_receipt_id,
            "source_freeze_authority_sha256": self.plan.freeze_authority_sha256,
            "source_project_head_id": self.plan.core_project_head_id,
            "source_project_head_manifest_sha256": (
                self.plan.core_project_head_manifest_sha256
            ),
            "context_artifact_ids": list(context),
            "evolution_targets_enabled": [],
            "frozen_composite_id": request["frozen_composite_id"],
            "frozen_composite_sha256": request["frozen_composite_sha256"],
            "official_policy": dict(OFFICIAL_DISABLED_POLICY),
            "attachment_ids": [],
            "evolution_job_ids": [],
            "frozen_project_fork_receipt_id": f"fork-receipt-{index:02d}",
            "frozen_project_fork_authority_sha256": "d" * 64,
            "seeded_destination_project_head_id": f"seeded-head-{index:02d}",
            "seeded_destination_project_head_manifest_sha256": "e" * 64,
            "frozen_project_fork_source_mutated": False,
            "frozen_project_fork_append_only": True,
            "owned_resource_ids": [],
            "owned_resources": [],
            **self.result_overrides,
        }
        self.destination_projects.append(destination)
        self.contexts.append(context)
        self._path(idempotency_key).write_text(
            json.dumps(
                {"request_sha256": canonical_sha256(request), "result": result},
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return result


class FakeUnifiedScorer:
    """Evaluator-only fake; its raw receipt stays under the private root."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True)
        self.execute_count = 0
        self.judge_call_count = 0
        self.escape_private_root = False
        self.tamper_private_hash = False

    def _result_path(self, idempotency_key: str) -> Path:
        digest = hashlib.sha256(idempotency_key.encode()).hexdigest()
        return self.root / f"{digest}.result.json"

    def recover_unified_score(
        self, _request: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any] | None:
        path = self._result_path(idempotency_key)
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def execute_unified_score(
        self, request: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]:
        if self._result_path(idempotency_key).exists():
            raise AssertionError("fake unified scorer executed twice")
        self.execute_count += 1
        private_path = self.root / f"{idempotency_key.replace(':', '-')}.private.json"
        if self.escape_private_root:
            private_path = self.root.parent / "escaped-private.json"
        private_path.write_text(
            json.dumps({"synthetic": True, "official_task_count": 40}),
            encoding="utf-8",
        )
        digest = hashlib.sha256(private_path.read_bytes()).hexdigest()
        result = {
            "scorer_receipt_id": "official-unified-score-fixture",
            "private_receipt_path": str(private_path),
            "private_receipt_sha256": "0" * 64 if self.tamper_private_hash else digest,
            "official_task_count": 40,
            "official_run_set_sha256": request["official_run_set_sha256"],
            "runs_per_attempt": 1,
            "pass_at_k": 1,
            "feedback_released": False,
            "artifact_updates_performed": 0,
            "scoring_complete": True,
            "raw_judge_outputs_private": True,
            "total_score": 0.0,
            "valid_artifact_count": 0,
            "invalid_artifact_count": 40,
            "owned_resource_ids": [],
            "owned_resources": [],
        }
        self._result_path(idempotency_key).write_text(
            json.dumps(result, sort_keys=True), encoding="utf-8"
        )
        return result


def _candidate_port(
    authority: FakeFrozenCandidateCore,
    plan: OfficialFrozenRunPlan,
) -> CoreFrozenOfficialCandidatePort:
    return CoreFrozenOfficialCandidatePort(
        authority=authority,
        source_project_id=plan.core_project_id,
        source_freeze_receipt_id=plan.freeze_receipt_id,
        source_freeze_authority_sha256=plan.freeze_authority_sha256,
        source_project_head_id=plan.core_project_head_id,
        source_project_head_manifest_sha256=plan.core_project_head_manifest_sha256,
        frozen_registry_artifacts=plan.frozen_registry_artifacts,
        core_generation="generation-official",
        release_identity="c" * 64,
    )


def test_core_candidate_port_enforces_independent_frozen_project(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    run_root = tmp_path / "official"
    request = _candidate_request(plan)
    authority = FakeFrozenCandidateCore(tmp_path / "core", plan, run_root=run_root)
    port = _candidate_port(authority, plan)
    assert port.preflight(request)["official_frozen_policy_ready"] is True
    result = port.execute(request, "candidate-0")
    assert result["destination_core_project_id"] != plan.core_project_id
    assert result["evolution_targets_enabled"] == []
    assert result["context_artifact_ids"] == [
        artifact["registry_id"] for artifact in plan.frozen_registry_artifacts.values()
    ]
    assert port.recover(request, "candidate-0") == result
    assert authority.execute_count == 1
    assert authority.model_call_count == 0


@pytest.mark.parametrize(
    ("override", "match"),
    [
        ({"destination_core_project_id": "project-community-final"}, "receipt"),
        ({"evolution_targets_enabled": ["agent_system"]}, "receipt"),
        ({"context_artifact_ids": ["wrong"]}, "receipt"),
    ],
)
def test_core_candidate_port_rejects_execution_authority_drift(
    tmp_path: Path,
    override: dict[str, Any],
    match: str,
) -> None:
    plan = _plan(tmp_path)
    authority = FakeFrozenCandidateCore(tmp_path / "core", plan)
    authority.result_overrides = override
    port = _candidate_port(authority, plan)
    request = _candidate_request(plan)
    with pytest.raises(ProductionOperationError, match=match):
        port.execute(request, "candidate-drift")


@pytest.mark.parametrize(
    "override",
    [
        {"core_generation": "wrong-generation"},
        {"release_identity": "d" * 64},
        {"source_project_head_manifest_sha256": "e" * 64},
    ],
)
def test_core_candidate_preflight_rejects_identity_drift(
    tmp_path: Path, override: dict[str, Any]
) -> None:
    plan = _plan(tmp_path)
    authority = FakeFrozenCandidateCore(tmp_path / "core", plan)
    authority.preflight_overrides = override
    port = _candidate_port(authority, plan)
    with pytest.raises(ProductionOperationError, match="PREFLIGHT"):
        port.preflight(_candidate_request(plan))
    assert authority.execute_count == 0


def test_official_validator_is_terminal_idempotent_and_never_invokes_judge(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "official"
    workspace = run_root / "runs" / "task-a0"
    for relative in ("code", "outputs", "report/images"):
        (workspace / relative).mkdir(parents=True, exist_ok=True)
    (workspace / "report/report.md").write_text("short", encoding="utf-8")
    (workspace / "_meta.json").write_text(
        json.dumps({"status": "completed", "exit_code": 0}), encoding="utf-8"
    )
    request = {
        "candidate": {
            "candidate_output_root": str(workspace),
            "completed": True,
        }
    }
    port = OfficialArtifactValidationPort(run_root)
    first = port.execute(request, "validator-0")
    second = port.execute(request, "validator-0")
    assert first == second
    assert first["artifact_valid"] is False
    assert first["terminal"] is True
    assert first["same_attempt_retry_allowed"] is False
    assert first["judge_invoked"] is False
    assert first["reflector_invoked"] is False
    assert first["evolution_invoked"] is False
    assert first["outputs_frozen_read_only"] is True
    for relative in ("code", "outputs", "report"):
        assert (workspace / relative).stat().st_mode & 0o222 == 0


def test_unified_scorer_is_exactly_once_private_and_fail_closed(tmp_path: Path) -> None:
    request = {"official_run_set_sha256": "f" * 64}
    authority = FakeUnifiedScorer(tmp_path / "private")
    port = OfficialUnifiedScorerPort(
        authority=authority,
        evaluator_private_root=tmp_path / "private",
    )
    first = port.execute(request, "score-once")
    second = port.execute(request, "score-once")
    assert first == second
    assert authority.execute_count == 1
    assert authority.judge_call_count == 0
    assert not {
        "raw_judge_response",
        "raw_judge_request",
        "judge_reasoning",
    }.intersection(first)

    escape = FakeUnifiedScorer(tmp_path / "escape-private")
    escape.escape_private_root = True
    escape_port = OfficialUnifiedScorerPort(
        authority=escape,
        evaluator_private_root=tmp_path / "escape-private",
    )
    with pytest.raises(ValueError, match="escaped"):
        escape_port.execute(request, "score-escape")

    tamper = FakeUnifiedScorer(tmp_path / "tamper-private")
    tamper.tamper_private_hash = True
    tamper_port = OfficialUnifiedScorerPort(
        authority=tamper,
        evaluator_private_root=tmp_path / "tamper-private",
    )
    with pytest.raises(ValueError, match="hash changed"):
        tamper_port.execute(request, "score-tamper")


def test_full_official40_concrete_ports_integration_has_no_training_state(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    run_root = tmp_path / "official-run"
    core = FakeFrozenCandidateCore(
        tmp_path / "core-authority",
        plan,
        run_root=run_root,
        failed_candidate_index=13,
    )
    scorer = FakeUnifiedScorer(tmp_path / "evaluator-private")
    ports = build_official_production_ports(
        candidate_authority=core,
        scorer_authority=scorer,
        run_root=run_root,
        evaluator_private_root=tmp_path / "evaluator-private",
        source_project_id=plan.core_project_id,
        source_freeze_receipt_id=plan.freeze_receipt_id,
        source_freeze_authority_sha256=plan.freeze_authority_sha256,
        source_project_head_id=plan.core_project_head_id,
        source_project_head_manifest_sha256=plan.core_project_head_manifest_sha256,
        frozen_registry_artifacts=plan.frozen_registry_artifacts,
        core_generation="generation-official",
        release_identity="c" * 64,
    )
    operations = ProductionOfficialTrainingOperations(
        experiment_run_id="official40-concrete-fixture",
        official_task_ids=plan.task_ids,
        frozen_protocol_sha256=plan.identity.protocol_sha256,
        frozen_composite_id=plan.frozen_composite_id,
        frozen_composite_sha256=plan.frozen_composite_sha256,
        receipt_root=run_root / "operation-receipts",
        ports=ports,
    )
    supervisor = OfficialTrainingSupervisor(
        store=OfficialTrainingStateStore(run_root / "state"),
        experiment_id="official40-concrete-fixture",
        plan=plan,
        operations=operations,
    )
    supervisor.initialize()
    for _ in range(500):
        if supervisor.status()["stage"] == OfficialTrainingStage.COMPLETE.value:
            break
        supervisor.run_next()
    state = supervisor.status()
    assert state["stage"] == OfficialTrainingStage.COMPLETE.value
    assert state["candidate_run_count"] == 40
    assert state["validator_terminal_count"] == 40
    assert state["task_manifests"][13]["task_terminal_status"] == (
        "CANDIDATE_FAILED_TERMINAL"
    )
    assert all(not item["artifact_valid"] for item in state["task_manifests"])
    assert len(set(core.destination_projects)) == 40
    assert all(project != plan.core_project_id for project in core.destination_projects)
    assert all(
        context
        == tuple(
            artifact["registry_id"]
            for artifact in plan.frozen_registry_artifacts.values()
        )
        for context in core.contexts
    )
    assert core.execute_count == 40
    assert core.model_call_count == 0
    assert scorer.execute_count == 1
    assert scorer.judge_call_count == 0
    assert supervisor.verify()["status"] == "PASS"
