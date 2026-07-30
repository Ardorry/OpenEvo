from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from openevo_researchclawbench.official_training_operations import (
    OFFICIAL_DISABLED_POLICY,
    OfficialProductionPorts,
    ProductionOfficialTrainingOperations,
)
from openevo_researchclawbench.official_training_supervisor import (
    OfficialFrozenRunPlan,
    OfficialTrainingIdentity,
    OfficialTrainingStage,
    OfficialTrainingStateStore,
    OfficialTrainingSupervisor,
    load_official_frozen_plan,
)
from openevo_researchclawbench.training_state_store import canonical_sha256

ROOT = Path(__file__).resolve().parents[4]
REAL_SPLIT = ROOT / "configs/community_split.yaml"
REAL_TASKS = ROOT / "ResearchClawBench/tasks"


def _identity() -> OfficialTrainingIdentity:
    return OfficialTrainingIdentity(
        protocol_sha256="1" * 64,
        core_identity_sha256="2" * 64,
        adapter_identity_sha256="3" * 64,
    )


def _final_state(*, identity: OfficialTrainingIdentity | None = None) -> dict[str, Any]:
    identity = identity or _identity()
    freeze = {
        "freeze_receipt_id": "freeze-official-source-1",
        "authority_sha256": "a" * 64,
        "core_authoritative": True,
        "frozen": True,
        "protocol_sha256": identity.protocol_sha256,
        "core_identity_sha256": identity.core_identity_sha256,
        "adapter_identity_sha256": identity.adapter_identity_sha256,
        "core_project_id": "project-frozen",
        "core_project_head_id": "head-frozen",
        "core_project_head_manifest_sha256": "4" * 64,
        "composite_id": "composite-frozen",
        "composite_sha256": "5" * 64,
        "agent_system": {
            "registry_id": "agent-system-frozen",
            "sha256": "6" * 64,
            "frozen": True,
        },
        "text_memory": {
            "registry_id": "text-memory-frozen",
            "sha256": "7" * 64,
            "frozen": True,
        },
        "skill_bundle": {
            "registry_id": "skill-bundle-frozen",
            "sha256": "8" * 64,
            "frozen": True,
        },
        **OFFICIAL_DISABLED_POLICY,
    }
    return {
        "stage": "FINAL_FROZEN",
        "_state_sha256": "9" * 64,
        "_protocol_sha256": identity.protocol_sha256,
        "_core_identity_sha256": identity.core_identity_sha256,
        "_adapter_identity_sha256": identity.adapter_identity_sha256,
        "final_freeze_receipt": freeze,
        "final_freeze_receipt_sha256": canonical_sha256(freeze),
    }


def _split(tmp_path: Path) -> tuple[Path, Path, str, tuple[str, ...]]:
    task_ids = tuple(f"Domain{index:02d}_{index:03d}" for index in range(40))
    task_root = tmp_path / "tasks"
    task_root.mkdir()
    for task_id in task_ids:
        (task_root / task_id).mkdir()
    split = tmp_path / "community_split.yaml"
    split.write_text(
        yaml.safe_dump({"official_test_tasks": list(task_ids)}, sort_keys=False),
        encoding="utf-8",
    )
    digest = hashlib.sha256(split.read_bytes()).hexdigest()
    return split, task_root, digest, task_ids


def _plan(tmp_path: Path) -> OfficialFrozenRunPlan:
    split, task_root, digest, _tasks = _split(tmp_path)
    return load_official_frozen_plan(
        split_config=split,
        expected_split_sha256=digest,
        official_task_root=task_root,
        final_state=_final_state(),
        identity=_identity(),
    )


class DurableFakePort:
    def __init__(
        self,
        root: Path,
        kind: str,
        handler: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        crash_once_after_write: bool = False,
    ) -> None:
        self.root = root / kind
        self.root.mkdir(parents=True, exist_ok=True)
        self.kind = kind
        self.handler = handler
        self.crash_once_after_write = crash_once_after_write

    def _path(self, key: str) -> Path:
        return self.root / (hashlib.sha256(key.encode()).hexdigest() + ".json")

    def _crash_marker(self) -> Path:
        return self.root / ".crashed-once"

    def preflight(self, _request: dict[str, Any]) -> dict[str, Any]:
        return {
            "service_reachable": True,
            "bearer_present": True,
            "bearer_valid": True,
            "generation_matches": True,
            "candidate_port_authorized": True,
            "official_frozen_policy_ready": True,
            "frozen_composite_matches": True,
            "registry_artifacts_frozen": True,
            "secret_recorded": False,
            "environment_fallback_used": False,
            "model_started": False,
        }

    def recover(
        self, _request: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any] | None:
        path = self._path(idempotency_key)
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def execute(
        self, request: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]:
        path = self._path(idempotency_key)
        if path.exists():
            raise AssertionError("fake external operation was executed twice")
        result = self.handler(request)
        path.write_text(json.dumps(result, sort_keys=True), encoding="utf-8")
        if self.crash_once_after_write and not self._crash_marker().exists():
            self._crash_marker().write_text("crashed", encoding="utf-8")
            raise RuntimeError(f"synthetic response loss after {self.kind}")
        return result

    def call_count(self) -> int:
        return len(list(self.root.glob("*.json")))


def _ports(
    root: Path,
    *,
    crash_candidate: bool = False,
    crash_scorer: bool = False,
) -> tuple[OfficialProductionPorts, dict[str, DurableFakePort]]:
    def candidate(request: dict[str, Any]) -> dict[str, Any]:
        index = int(request["task_index"])
        output = root / "runs" / request["run_id"]
        output.mkdir(parents=True, exist_ok=True)
        return {
            "task_id": request["task_id"],
            "run_id": request["run_id"],
            "attempt_index": 0,
            "session_id": f"session-{index:02d}",
            "dataset_id": f"dataset-{index:02d}",
            "dataset_revision": f"dataset-{index:02d}.v1",
            "candidate_output_root": str(output),
            "runtime_seconds": 1.0,
            "completed": True,
            "sealed": True,
            "fresh_workspace": True,
            "frozen_composite_id": request["frozen_composite_id"],
            "frozen_composite_sha256": request["frozen_composite_sha256"],
            "official_policy": request["official_policy"],
            "owned_resource_ids": [],
            "owned_resources": [],
        }

    def validation(request: dict[str, Any]) -> dict[str, Any]:
        index = int(request["task_index"])
        body = {
            "task_id": request["task_id"],
            "artifact_valid": index != 7,
        }
        return {
            "artifact_valid": index != 7,
            "artifact_root_sha256": canonical_sha256(body),
            "validator_receipt_id": f"validator-{index:02d}",
            "validator_receipt_sha256": canonical_sha256(body),
            "terminal": True,
            "judge_invoked": False,
            "reflector_invoked": False,
            "evolution_invoked": False,
        }

    def scorer(request: dict[str, Any]) -> dict[str, Any]:
        manifests = request["run_manifests"]
        return {
            "scorer_receipt_id": "official-unified-scorer-1",
            "official_task_count": 40,
            "official_run_set_sha256": request["official_run_set_sha256"],
            "runs_per_attempt": 1,
            "pass_at_k": 1,
            "feedback_released": False,
            "artifact_updates_performed": 0,
            "scoring_complete": True,
            "raw_judge_outputs_private": True,
            "valid_artifact_count": sum(item["artifact_valid"] for item in manifests),
            "invalid_artifact_count": sum(not item["artifact_valid"] for item in manifests),
            "total_score": 39.0,
        }

    authorities = {
        "candidate": DurableFakePort(
            root, "candidate", candidate, crash_once_after_write=crash_candidate
        ),
        "validation": DurableFakePort(root, "validation", validation),
        "scorer": DurableFakePort(
            root, "scorer", scorer, crash_once_after_write=crash_scorer
        ),
    }
    return OfficialProductionPorts(**authorities), authorities


def _build(
    tmp_path: Path,
    *,
    crash_candidate: bool = False,
    crash_scorer: bool = False,
) -> tuple[OfficialTrainingSupervisor, dict[str, DurableFakePort], OfficialFrozenRunPlan]:
    plan = _plan(tmp_path)
    ports, authorities = _ports(
        tmp_path / "external",
        crash_candidate=crash_candidate,
        crash_scorer=crash_scorer,
    )
    operations = ProductionOfficialTrainingOperations(
        experiment_run_id="rcb_oe_v0_official40_frozen_test",
        official_task_ids=plan.task_ids,
        frozen_protocol_sha256=plan.identity.protocol_sha256,
        frozen_composite_id=plan.frozen_composite_id,
        frozen_composite_sha256=plan.frozen_composite_sha256,
        receipt_root=tmp_path / "operation_receipts",
        ports=ports,
    )
    supervisor = OfficialTrainingSupervisor(
        store=OfficialTrainingStateStore(tmp_path / "state"),
        experiment_id="rcb_oe_v0_official40_frozen_test",
        plan=plan,
        operations=operations,
    )
    return supervisor, authorities, plan


def _restart(
    tmp_path: Path,
    plan: OfficialFrozenRunPlan,
    authorities: dict[str, DurableFakePort],
) -> OfficialTrainingSupervisor:
    operations = ProductionOfficialTrainingOperations(
        experiment_run_id="rcb_oe_v0_official40_frozen_test",
        official_task_ids=plan.task_ids,
        frozen_protocol_sha256=plan.identity.protocol_sha256,
        frozen_composite_id=plan.frozen_composite_id,
        frozen_composite_sha256=plan.frozen_composite_sha256,
        receipt_root=tmp_path / "operation_receipts",
        ports=OfficialProductionPorts(**authorities),
    )
    return OfficialTrainingSupervisor(
        store=OfficialTrainingStateStore(tmp_path / "state"),
        experiment_id="rcb_oe_v0_official40_frozen_test",
        plan=plan,
        operations=operations,
    )


def test_real_frozen_split_contains_40_available_official_tasks() -> None:
    expected = hashlib.sha256(REAL_SPLIT.read_bytes()).hexdigest()
    plan = load_official_frozen_plan(
        split_config=REAL_SPLIT,
        expected_split_sha256=expected,
        official_task_root=REAL_TASKS,
        final_state=_final_state(),
        identity=_identity(),
    )
    assert len(plan.task_ids) == 40
    assert len(set(plan.task_ids)) == 40
    assert plan.task_ids[0] == "Astronomy_000"
    assert plan.task_ids[-1] == "Physics_003"


def test_official_run_until_complete_stops_on_blocked_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor, _authorities, _plan_value = _build(tmp_path)
    monkeypatch.setattr(supervisor, "status", lambda: {"stage": "BLOCKED"})
    monkeypatch.setattr(
        supervisor,
        "run_next",
        lambda: (_ for _ in ()).throw(AssertionError("terminal state replayed")),
    )
    assert supervisor.run_until_complete() == {"stage": "BLOCKED"}


def test_plan_rejects_nonfinal_or_non_core_authoritative_source(tmp_path: Path) -> None:
    split, task_root, digest, _tasks = _split(tmp_path)
    nonfinal = _final_state()
    nonfinal["stage"] = "FINAL_FREEZE_PENDING"
    with pytest.raises(ValueError, match="FINAL_FROZEN"):
        load_official_frozen_plan(
            split_config=split,
            expected_split_sha256=digest,
            official_task_root=task_root,
            final_state=nonfinal,
            identity=_identity(),
        )
    untrusted = _final_state()
    untrusted["final_freeze_receipt"]["core_authoritative"] = False
    with pytest.raises(ValueError, match="Core-authoritative"):
        load_official_frozen_plan(
            split_config=split,
            expected_split_sha256=digest,
            official_task_root=task_root,
            final_state=untrusted,
            identity=_identity(),
        )


def test_split_hash_and_exact_task_inventory_are_fail_closed(tmp_path: Path) -> None:
    split, task_root, _digest, tasks = _split(tmp_path)
    with pytest.raises(ValueError, match="split identity drifted"):
        load_official_frozen_plan(
            split_config=split,
            expected_split_sha256="0" * 64,
            official_task_root=task_root,
            final_state=_final_state(),
            identity=_identity(),
        )
    split.write_text(
        yaml.safe_dump({"official_test_tasks": [*tasks[:-1], tasks[0]]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="40 distinct"):
        load_official_frozen_plan(
            split_config=split,
            expected_split_sha256=hashlib.sha256(split.read_bytes()).hexdigest(),
            official_task_root=task_root,
            final_state=_final_state(),
            identity=_identity(),
        )


def test_full_official40_fake_integration_is_restart_safe_and_scores_once(
    tmp_path: Path,
) -> None:
    supervisor, authorities, plan = _build(tmp_path)
    supervisor.initialize()
    for _ in range(500):
        state = supervisor.status()
        if state["stage"] == OfficialTrainingStage.COMPLETE.value:
            break
        supervisor.run_next()
        supervisor = _restart(tmp_path, plan, authorities)
    state = supervisor.status()
    assert state["stage"] == OfficialTrainingStage.COMPLETE.value
    assert state["candidate_run_count"] == 40
    assert state["validator_terminal_count"] == 40
    assert len(state["task_manifests"]) == 40
    assert authorities["candidate"].call_count() == 40
    assert authorities["validation"].call_count() == 40
    assert authorities["scorer"].call_count() == 1
    assert state["task_manifests"][7]["task_terminal_status"] == "VALIDATOR_FAILED_TERMINAL"
    assert all(
        manifest["frozen_composite_sha256"] == plan.frozen_composite_sha256
        for manifest in state["task_manifests"]
    )
    assert all(
        manifest[key] is expected
        for manifest in state["task_manifests"]
        for key, expected in OFFICIAL_DISABLED_POLICY.items()
    )
    assert state["scorer_receipt"]["official_task_count"] == 40
    assert state["scorer_receipt"]["invalid_artifact_count"] == 1
    assert supervisor.verify()["status"] == "PASS"


def test_scorer_is_withheld_before_all_40_are_sealed(tmp_path: Path) -> None:
    supervisor, authorities, _plan_value = _build(tmp_path)
    supervisor.initialize()
    operations = supervisor.operations
    with pytest.raises(RuntimeError, match="WITHHELD_UNTIL_40"):
        operations.score_all(
            {
                "run_manifests": [],
                "official_policy": dict(OFFICIAL_DISABLED_POLICY),
                "runs_per_attempt": 1,
                "pass_at_k": 1,
                "scoring_mode": "unified_after_all_40_sealed",
            },
            "score-too-early",
        )
    assert authorities["scorer"].call_count() == 0


def test_candidate_and_scorer_response_loss_do_not_repeat_paid_side_effects(
    tmp_path: Path,
) -> None:
    supervisor, authorities, plan = _build(
        tmp_path, crash_candidate=True, crash_scorer=True
    )
    supervisor.initialize()
    supervisor.run_next()  # task ready
    supervisor.run_next()  # candidate intent
    with pytest.raises(RuntimeError, match="response loss after candidate"):
        supervisor.run_next()
    assert authorities["candidate"].call_count() == 1
    supervisor = _restart(tmp_path, plan, authorities)
    supervisor.run_next()
    assert authorities["candidate"].call_count() == 1
    for _ in range(500):
        if supervisor.status()["stage"] == OfficialTrainingStage.SCORING_RUNNING.value:
            break
        supervisor.run_next()
        supervisor = _restart(tmp_path, plan, authorities)
    with pytest.raises(RuntimeError, match="response loss after scorer"):
        supervisor.run_next()
    assert authorities["scorer"].call_count() == 1
    supervisor = _restart(tmp_path, plan, authorities)
    final = supervisor.run_next()
    assert final["stage"] == OfficialTrainingStage.COMPLETE.value
    assert authorities["scorer"].call_count() == 1
    assert supervisor.verify()["scorer_operation_count"] == 1


def test_official_operation_surface_has_no_training_mutations(tmp_path: Path) -> None:
    supervisor, _authorities, _plan_value = _build(tmp_path)
    operations = supervisor.operations
    for forbidden in (
        "attach_feedback",
        "create_feedback_attachment",
        "evolve_artifacts",
        "prepare_successor",
        "collect_artifact_jobs",
        "construct_composite",
        "destroy_task_local",
        "sanitize_cross_task",
    ):
        assert not hasattr(operations, forbidden)


def test_candidate_request_rejects_overlay_or_evolution_state(tmp_path: Path) -> None:
    supervisor, authorities, plan = _build(tmp_path)
    operations = supervisor.operations
    request = {
        "experiment_id": supervisor.experiment_id,
        "task_id": plan.task_ids[0],
        "task_index": 0,
        "attempt_index": 0,
        "run_id": "unsafe-official-run",
        "fresh_workspace": True,
        "resume_in_place": False,
        "frozen_protocol_sha256": plan.identity.protocol_sha256,
        "frozen_composite_id": plan.frozen_composite_id,
        "frozen_composite_sha256": plan.frozen_composite_sha256,
        "official_policy": dict(OFFICIAL_DISABLED_POLICY),
        "task_local_overlay_id": "forbidden-overlay",
    }
    with pytest.raises(ValueError, match="contains training state"):
        operations.candidate_readiness(request)
    assert authorities["candidate"].call_count() == 0


def test_state_reopen_rejects_plan_identity_drift(tmp_path: Path) -> None:
    supervisor, _authorities, plan = _build(tmp_path)
    supervisor.initialize()
    drifted = OfficialFrozenRunPlan(
        **{
            **plan.__dict__,
            "frozen_composite_sha256": "a" * 64,
        }
    )
    reopened = OfficialTrainingSupervisor(
        store=OfficialTrainingStateStore(tmp_path / "state"),
        experiment_id=supervisor.experiment_id,
        plan=drifted,
        operations=supervisor.operations,
    )
    with pytest.raises(ValueError, match="identity drifted"):
        reopened.status()
