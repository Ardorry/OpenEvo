from __future__ import annotations

import hashlib
import json
import tarfile
from copy import deepcopy
from io import BytesIO
from pathlib import Path

import openevo_researchclawbench.candidate_reconciliation as reconciliation_module
import openevo_researchclawbench.production_operation_ports as ports_module
import pytest
import yaml
from openevo_researchclawbench.candidate_reconciliation import (
    CandidateReconciliationRegistry,
    CandidateReconciliationSpec,
    _source_authority,
    reconcile_candidate_sealed_authority,
)
from openevo_researchclawbench.config import ExperimentConfig
from openevo_researchclawbench.managed_core_control import ManagedCoreControlAuthority
from openevo_researchclawbench.production_operation_ports import (
    CoreControlError,
    CoreV2CandidateReconciliationPort,
)
from openevo_researchclawbench.production_training_operations import (
    ProductionOperationPort,
    ProductionPorts,
)
from openevo_researchclawbench.training_state_store import TrainingStateStore
from openevo_researchclawbench.training_supervisor import (
    CandidateAuthorityUnavailable,
    CommunityTrainingSupervisor,
    SupervisorIdentity,
)
from openevo_researchclawbench.transition_engine import TrainingStage

ROOT = Path(__file__).resolve().parents[4]
PROTOCOL = ROOT / "experiments/sequential_task_reflector_evolution_v0/protocol/protocol.yaml"
SOURCE = "rcb_oe_v0_reconciliation_source"
TARGET = "rcb_oe_v0_reconciliation_target_reconcile"
IDENTITY = SupervisorIdentity("1" * 64, "2" * 64, "3" * 64)


class _SourceFailureOperations:
    def candidate_readiness(self, _request):
        return {
            "service_reachable": True,
            "bearer_present": True,
            "bearer_valid": True,
            "generation_matches": True,
            "candidate_port_authorized": True,
            "secret_recorded": False,
            "generation": "4" * 32,
            "release_identity": "5" * 64,
            "service_identity_id": "core-service-test",
        }

    def ensure_candidate(self, _request, _key):
        raise CandidateAuthorityUnavailable("publication race")


class _ClosedPort(ProductionOperationPort):
    def recover(self, _request, _key):
        return None

    def execute(self, _request, _key):
        return {}


class _ReconciliationPort(ProductionOperationPort):
    def __init__(self) -> None:
        self.calls = 0

    def recover(self, _request, _key):
        return None

    def execute(self, request, _key):
        self.calls += 1
        return {
            "session_id": request["source_session_id"],
            "dataset_id": request["source_dataset_id"],
            "dataset_revision": request["source_dataset_revision"],
            "completed": True,
            "run_id": request["source_run_id"],
            "runtime_seconds": 1.0,
            "core_project_id": request["source_core_project_id"],
            "core_task_id": request["source_core_task_id"],
            "core_attempt_id": request["source_core_attempt_id"],
            "workspace_binding_id": "workspace-binding",
            "task_request_id": "task-request",
            "session_result_id": request["source_session_result_sha256"],
            "transcript_receipt": {"sha256": request["source_session_result_sha256"]},
            "candidate_output_root": "closed-output-root",
            "source_namespace": request["source_namespace"],
            "source_state_sha256": request["source_state_sha256"],
            "seal_origin": "reconciliation",
            "candidate_reexecuted": False,
            "additional_candidate_model_calls": 0,
            "owned_resources": [],
        }


class _DownstreamOperations:
    def __init__(self) -> None:
        self.candidate_calls = 0
        self.validator_calls = 0
        self.evaluator_calls = 0

    def ensure_candidate(self, _request, _key):
        self.candidate_calls += 1
        raise AssertionError("a reconciled candidate must not execute again")

    def validate_artifact(self, _request, _key):
        self.validator_calls += 1
        return {
            "artifact_valid": True,
            "completeness": 1,
            "artifact_root_sha256": "a" * 64,
        }

    def evaluate(self, _request, _key):
        self.evaluator_calls += 1
        return {"total_score": 0.25}


class _CurrentCandidateReconciliationOperations:
    def __init__(self) -> None:
        self.calls = 0

    def reconcile_candidate_authority(self, request, _key):
        self.calls += 1
        return {
            "session_id": request["source_session_id"],
            "dataset_id": request["source_dataset_id"],
            "dataset_revision": request["source_dataset_revision"],
            "completed": True,
            "run_id": request["source_run_id"],
            "runtime_seconds": 1.0,
            "core_project_id": request["source_core_project_id"],
            "core_task_id": request["source_core_task_id"],
            "core_attempt_id": request["source_core_attempt_id"],
            "workspace_binding_id": "workspace-binding-a1",
            "task_request_id": "task-request-a1",
            "session_result_id": request["source_session_result_sha256"],
            "transcript_receipt": {
                "sha256": request["source_session_result_sha256"]
            },
            "runtime_injection": {"artifact_count": 3},
            "input_composite_id": request["source_input_composite_id"],
            "input_project_head_id": request["source_input_project_head_id"],
            "task_local_overlay_id": request["source_task_local_overlay_id"],
            "source_namespace": request["source_namespace"],
            "source_state_sha256": request["source_state_sha256"],
            "seal_origin": "reconciliation",
            "candidate_reexecuted": False,
            "additional_candidate_model_calls": 0,
            "owned_resources": [],
        }


def _config(tmp_path: Path) -> ExperimentConfig:
    raw = yaml.safe_load(PROTOCOL.read_text(encoding="utf-8"))
    project = tmp_path / "project"
    experiment = project / "experiments/sequential_task_reflector_evolution_v0"
    rcb = project / "ResearchClawBench"
    experiment.mkdir(parents=True)
    rcb.mkdir()
    protocol = experiment / "protocol.yaml"
    raw["paths"] = {
        "project_root": str(project),
        "experiment_root": str(experiment),
        "researchclawbench_root": str(rcb),
    }
    raw["source_identity"] = {
        "openevo_core_source_tree_sha256": IDENTITY.core_identity_sha256,
        "adapter_tree_sha256": IDENTITY.adapter_identity_sha256,
    }
    protocol.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return ExperimentConfig(protocol, raw)


def _source_supervisor(config: ExperimentConfig) -> CommunityTrainingSupervisor:
    root = config.experiment_root / "supervisor" / SOURCE
    supervisor = CommunityTrainingSupervisor(
        store=TrainingStateStore(root),
        experiment_id=SOURCE,
        identity=IDENTITY,
        operations=_SourceFailureOperations(),
        task_ids=("Astronomy_004",),
    )
    supervisor.initialize()
    supervisor.run_next()
    supervisor.run_next()
    supervisor.run_next()
    assert supervisor.status()["stage"] == "BLOCKED"
    return supervisor


def _write_json(path: Path, value: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _spec(config: ExperimentConfig, supervisor: CommunityTrainingSupervisor) -> CandidateReconciliationSpec:
    source = supervisor.status()
    evidence = config.experiment_root / "reconciliation-evidence"
    classification = {
        "run_id": SOURCE,
        "adapter_failure": "DATASET_SEALED_TIMELINE_PUBLICATION_RACE",
        "protocol_retry_budget_exhausted": True,
        "sealed_attempt_must_not_be_overwritten": True,
        "new_model_call_required_for_recovery": False,
        "session_id": "session-source",
        "completed_dataset_id": "dataset-source",
        "completed_dataset_revision": "dataset-revision.v1",
    }
    classification_path = evidence / "classification.json"
    active_path = evidence / "source-active.json"
    readiness_path = evidence / "source-readiness.json"
    migration_active = evidence / "migration-active.json"
    migration_readiness = evidence / "migration-readiness.json"
    classification_sha = _write_json(classification_path, classification)
    active_sha = _write_json(active_path, {"source": True})
    readiness_sha = _write_json(readiness_path, {"source": True})
    migration_active_sha = _write_json(migration_active, {"migration": True})
    protocol_sha = hashlib.sha256(config.path.read_bytes()).hexdigest()
    migration_readiness_sha = _write_json(
        migration_readiness,
        {
            "ready": True,
            "blockers": [],
            "protocol_sha256": protocol_sha,
            "core_control": {
                "generation": "4" * 32,
                "release_identity": "5" * 64,
            },
        },
    )
    return CandidateReconciliationSpec(
        operation_id="reconcile-source-once",
        source_namespace=SOURCE,
        target_namespace=TARGET,
        source_task_id="Astronomy_004",
        source_attempt_index=0,
        source_run_id="Astronomy_004_a0_v2",
        source_core_project_id="project-source",
        source_core_task_id="task-source",
        source_core_attempt_id="attempt-source",
        source_session_id="session-source",
        source_dataset_id="dataset-source",
        source_dataset_revision="dataset-revision.v1",
        source_session_result_sha256="6" * 64,
        source_state_sha256=source["_state_sha256"],
        source_protocol_identity=IDENTITY.protocol_sha256,
        source_active_identity=active_sha,
        source_readiness_identity=readiness_sha,
        source_adapter_identity=IDENTITY.adapter_identity_sha256,
        source_core_identity=IDENTITY.core_identity_sha256,
        source_failure_classification_path=str(classification_path),
        source_failure_classification_sha256=classification_sha,
        source_active_identity_path=str(active_path),
        source_readiness_path=str(readiness_path),
        migration_active_identity_path=str(migration_active),
        migration_active_identity_sha256=migration_active_sha,
        migration_readiness_path=str(migration_readiness),
        migration_readiness_sha256=migration_readiness_sha,
        expected_model="gpt-5.5",
        expected_harness="codex",
        expected_capture_mode="transcript",
        expected_codex_version="0.144.1",
        migration_reason="adapter_dataset_publication_race",
    )


def _authority() -> ManagedCoreControlAuthority:
    from pydantic import SecretStr

    return ManagedCoreControlAuthority(
        base_url="http://127.0.0.1:1234",
        service_identity_id="core-service-test",
        generation="4" * 32,
        release_identity="5" * 64,
        registry_digest="7" * 64,
        source_commit="8" * 40,
        status_proof="9" * 64,
        attached=True,
        managed_host_profile_ready=True,
        _bearer=SecretStr("not-exported"),
    )


def _ports(reconciliation: ProductionOperationPort) -> ProductionPorts:
    closed = _ClosedPort()
    return ProductionPorts(
        candidate=closed,
        validation=closed,
        evaluation=closed,
        attachment=closed,
        evolution=closed,
        composite=closed,
        task_local=closed,
        sanitizer=closed,
        freeze=closed,
        reconciliation=reconciliation,
    )


def test_reconciliation_controller_creates_one_sealed_successor_without_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    source = _source_supervisor(config)
    spec = _spec(config, source)
    port = _ReconciliationPort()
    monkeypatch.setattr(
        reconciliation_module,
        "build_production_ports",
        lambda *_args, **_kwargs: _ports(port),
    )
    source_before = source.status()
    first = reconcile_candidate_sealed_authority(
        config=config, spec=spec, core_authority=_authority()
    )
    second = reconcile_candidate_sealed_authority(
        config=config, spec=spec, core_authority=_authority()
    )
    assert first == second
    assert port.calls == 1
    assert first["migration"]["candidate_reexecuted"] is False
    assert first["migration"]["additional_model_calls"] == 0
    target_store = TrainingStateStore(config.experiment_root / "supervisor" / TARGET)
    target = target_store.load(TARGET)
    assert target["stage"] == "CANDIDATE_SEALED"
    assert target["namespace_type"] == "reconciliation_successor"
    assert target["session_ids"] == ["session-source"]
    assert target["dataset_ids"] == ["dataset-source"]
    assert source.status()["_state_sha256"] == source_before["_state_sha256"]
    assert source.status()["stage"] == "BLOCKED"
    downstream = _DownstreamOperations()
    resumed = CommunityTrainingSupervisor(
        store=target_store,
        experiment_id=TARGET,
        identity=SupervisorIdentity(
            hashlib.sha256(config.path.read_bytes()).hexdigest(),
            IDENTITY.core_identity_sha256,
            IDENTITY.adapter_identity_sha256,
        ),
        operations=downstream,
        task_ids=("Astronomy_004",),
    )
    resumed.run_next()
    resumed.run_next()
    resumed.run_next()
    assert resumed.status()["stage"] == "EVALUATED"
    assert downstream.candidate_calls == 0
    assert downstream.validator_calls == 1
    assert downstream.evaluator_calls == 1


def test_current_attempt_reconciliation_preserves_continuation_and_does_not_replay(
    tmp_path: Path,
) -> None:
    store = TrainingStateStore(tmp_path / "state")
    operations = _CurrentCandidateReconciliationOperations()
    supervisor = CommunityTrainingSupervisor(
        store=store,
        experiment_id="rcb_oe_v0_current_candidate_reconcile",
        identity=IDENTITY,
        operations=operations,
        task_ids=("Astronomy_004",),
    )
    initial = supervisor._initial_state()
    store.initialize_experiment(
        experiment_id=supervisor.experiment_id,
        protocol_sha256=IDENTITY.protocol_sha256,
        core_identity_sha256=IDENTITY.core_identity_sha256,
        adapter_identity_sha256=IDENTITY.adapter_identity_sha256,
        initial_state=initial,
    )
    store.transition(
        experiment_id=supervisor.experiment_id,
        idempotency_key=f"{supervisor.experiment_id}:test:attempt-ready",
        source=TrainingStage.INITIALIZED,
        target=TrainingStage.TASK_ATTEMPT_READY,
        updates={
            "current_attempt": 1,
            "current_composite_id": "composite-c1",
            "composite_ids": ["c000", "composite-c1"],
            "session_ids": ["session-a0"],
            "dataset_ids": ["dataset-a0"],
            "active_admission_receipt": {
                "core_project_head_id": "head-c1"
            },
            "current_task_local_overlay_id": "overlay-a0",
        },
        receipt={"test_fixture": True},
    )
    source = {"namespace": "rcb_oe_v0_source", "source_key": "9" * 64}
    request = {
        "task_id": "Astronomy_004",
        "source_namespace": source["namespace"],
        "source_state_sha256": "8" * 64,
        "source_task_id": "Astronomy_004",
        "source_attempt_index": 1,
        "source_run_id": "Astronomy_004_a1_v12",
        "source_core_project_id": "project-source",
        "source_core_task_id": "task-a1",
        "source_core_attempt_id": "attempt-a1",
        "source_session_id": "session-a1",
        "source_dataset_id": "dataset-a1",
        "source_dataset_revision": "dataset-a1.v1",
        "source_session_result_sha256": "7" * 64,
        "source_input_composite_id": "composite-c1",
        "source_input_project_head_id": "head-c1",
        "source_task_local_overlay_id": "overlay-a0",
    }
    first = supervisor.reconcile_current_candidate_sealed(
        source=source, request=request
    )
    second = supervisor.reconcile_current_candidate_sealed(
        source=source, request=request
    )
    assert first == second
    assert operations.calls == 1
    assert first["stage"] == "CANDIDATE_SEALED"
    assert first["current_attempt"] == 1
    assert first["session_ids"] == ["session-a0", "session-a1"]
    assert first["dataset_ids"] == ["dataset-a0", "dataset-a1"]
    assert first["current_composite_id"] == "composite-c1"
    assert first["candidate_reexecuted"] is False


def test_completed_invalid_validator_can_be_terminally_reconciled_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    source = _source_supervisor(config)
    spec = _spec(config, source)
    port = _ReconciliationPort()
    monkeypatch.setattr(
        reconciliation_module,
        "build_production_ports",
        lambda *_args, **_kwargs: _ports(port),
    )
    reconcile_candidate_sealed_authority(
        config=config, spec=spec, core_authority=_authority()
    )
    target_store = TrainingStateStore(config.experiment_root / "supervisor" / TARGET)
    sealed = target_store.load(TARGET)
    source_identity = SupervisorIdentity(
        sealed["_protocol_sha256"],
        sealed["_core_identity_sha256"],
        sealed["_adapter_identity_sha256"],
    )
    validator_key = f"{TARGET}:Astronomy_004:a0:validator"
    validation = {
        "artifact_valid": False,
        "completeness": 15,
        "artifact_root_sha256": None,
        "validator_errors": ["REPORT_REQUIRED_SECTIONS_MISSING"],
        "content_sha256": "d" * 64,
    }
    target_store.plan_side_effect(
        experiment_id=TARGET,
        idempotency_key=validator_key,
        kind="validator",
        request={"candidate": sealed["active_candidate_receipt"]},
    )
    target_store.complete_side_effect(
        idempotency_key=validator_key,
        receipt=validation,
    )
    executor_identity = SupervisorIdentity("a" * 64, "2" * 64, "b" * 64)
    repair = CommunityTrainingSupervisor(
        store=target_store,
        experiment_id=TARGET,
        identity=executor_identity,
        operations=_DownstreamOperations(),
        task_ids=("Astronomy_004",),
    )
    first = repair.reconcile_invalid_validator_terminal(
        operation_id="validator-reconcile-test",
        source_identity=source_identity,
        source_state_sha256=sealed["_state_sha256"],
        expected_validator_receipt_sha256="d" * 64,
        executor_identity=executor_identity,
        executor_active_identity_sha256="e" * 64,
        executor_readiness_sha256="f" * 64,
    )
    second = repair.reconcile_invalid_validator_terminal(
        operation_id="validator-reconcile-test",
        source_identity=source_identity,
        source_state_sha256=sealed["_state_sha256"],
        expected_validator_receipt_sha256="d" * 64,
        executor_identity=executor_identity,
        executor_active_identity_sha256="e" * 64,
        executor_readiness_sha256="f" * 64,
    )
    assert first == second
    assert first["stage"] == "FAILED"
    assert first["failure_reason"] == "ARTIFACT_VALIDATOR_FAILED"
    assert target_store.active_resources(TARGET) == []


def test_reconciliation_registry_rejects_second_operation_for_same_source(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    source = _source_supervisor(config)
    spec = _spec(config, source)
    registry = CandidateReconciliationRegistry(tmp_path / "registry")
    assert registry.reserve(spec)["status"] == "planned"
    assert registry.reserve(spec)["status"] == "planned"
    other = CandidateReconciliationSpec(
        **{**spec.__dict__, "operation_id": "reconcile-source-different"}
    )
    with pytest.raises(ValueError, match="already reconciled differently"):
        registry.reserve(other)


def test_reconciliation_supervisor_rejects_ordinary_namespace(tmp_path: Path) -> None:
    store = TrainingStateStore(tmp_path / "state")
    supervisor = CommunityTrainingSupervisor(
        store=store,
        experiment_id="ordinary-training",
        identity=IDENTITY,
        operations=_SourceFailureOperations(),
        task_ids=("Astronomy_004",),
    )
    with pytest.raises(ValueError, match="reconciliation namespace"):
        supervisor.reconcile_candidate_sealed(
            source={"namespace": SOURCE, "source_key": "a" * 64},
            request={},
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"migration_reason": "not-allowlisted"}, "reason"),
        ({"source_attempt_index": 1}, "task/attempt"),
        ({"target_namespace": "rcb_oe_v0_plain"}, "namespace"),
        ({"source_session_result_sha256": "bad"}, "SHA-256"),
    ],
)
def test_reconciliation_spec_rejects_open_or_invalid_identity(
    tmp_path: Path, mutation: dict, message: str
) -> None:
    config = _config(tmp_path)
    source = _source_supervisor(config)
    spec = _spec(config, source)
    changed = CandidateReconciliationSpec(**{**spec.__dict__, **mutation})
    with pytest.raises(ValueError, match=message):
        changed.validate()


@pytest.mark.parametrize(
    "source_mutation",
    [
        "wrong_failure",
        "source_not_terminal",
        "active_resource",
        "attempt_receipt",
        "retry_not_exhausted",
        "identity_incomplete",
    ],
)
def test_source_reconciliation_closure_rejects_invalid_source(
    tmp_path: Path, source_mutation: str
) -> None:
    config = _config(tmp_path)
    source = _source_supervisor(config)
    spec = _spec(config, source)
    if source_mutation == "wrong_failure":
        spec = CandidateReconciliationSpec(
            **{**spec.__dict__, "source_state_sha256": "0" * 64}
        )
    elif source_mutation == "source_not_terminal":
        other = CommunityTrainingSupervisor(
            store=TrainingStateStore(config.experiment_root / "supervisor" / "rcb_oe_v0_other"),
            experiment_id="rcb_oe_v0_other",
            identity=IDENTITY,
            operations=_SourceFailureOperations(),
            task_ids=("Astronomy_004",),
        )
        other.initialize()
        spec = CandidateReconciliationSpec(
            **{
                **spec.__dict__,
                "source_namespace": "rcb_oe_v0_other",
                "source_state_sha256": other.status()["_state_sha256"],
            }
        )
    elif source_mutation == "active_resource":
        source.store.register_resource(
            experiment_id=SOURCE,
            resource_id="owned-process",
            kind="process",
            identity={"pid": 123, "start_ticks": 456},
        )
    elif source_mutation == "attempt_receipt":
        source.store.record_attempt(
            SOURCE,
            {"task_id": "Astronomy_004", "attempt_index": 0},
        )
    elif source_mutation == "retry_not_exhausted":
        path = Path(spec.source_failure_classification_path)
        value = json.loads(path.read_text())
        value["protocol_retry_budget_exhausted"] = False
        spec = CandidateReconciliationSpec(
            **{
                **spec.__dict__,
                "source_failure_classification_sha256": _write_json(path, value),
            }
        )
    else:
        spec = CandidateReconciliationSpec(
            **{**spec.__dict__, "source_adapter_identity": "0" * 64}
        )
    with pytest.raises(ValueError):
        _source_authority(config, spec)


def _tar_payload() -> bytes:
    stream = BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, payload in (
            ("code/analysis.py", b"print('sealed')\n"),
            ("outputs/result.txt", b"sealed-result\n"),
            ("report/report.md", b"# Sealed report\n"),
            ("report/images/figure.png", b"not-a-real-png"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, BytesIO(payload))
    return stream.getvalue()


def _core_fixture() -> dict:
    payload = _tar_payload()
    return {
        "archive": payload,
        "attempt": {
            "execution_receipt": {
                "session_id": "session-source",
                "session_result_sha256": "6" * 64,
                "terminal_status": "COMPLETED",
                "model_ref": "gpt-5.5",
                "harness_id": "codex",
                "capture_mode": "transcript",
                "workspace_handoff_id": "workspace-binding",
                "rollout_payload_sha256": "task-request",
            },
            "session_result": {
                "session_id": "session-source",
                "status": "COMPLETED",
                "metadata": {
                    "agent": {"model_name": "gpt-5.5"},
                    "openevo": {
                        "credential_isolation": {"codex_version": "0.144.1"}
                    },
                },
                "trajectory": {"traces": [{"finish_reason": "stop"}]},
                "timing": {"run_ms": 1000.0},
                "workspace_result": {
                    "output_archive": {
                        "content_sha256": hashlib.sha256(payload).hexdigest()
                    }
                },
            },
        },
        "task": {
            "task_id": "task-source",
            "state": "failed",
            "authoritative_attempt_id": "attempt-source",
            "attempts": [{"attempt_id": "attempt-source"}],
            "admission": {
                "predecessor_project_head": {"project_head_id": "head-source"}
            },
            "successor_transition": {
                "successor_transition_id": "successor-source"
            },
        },
        "project": {
            "project_id": "project-source",
            "config": {"task": {"title": "Astronomy_004"}},
        },
        "dataset": {
            "session_id": "session-source",
            "completed_dataset_id": "dataset-source",
            "completed_dataset_revision": "dataset-revision.v1",
        },
        "timeline": {
            "items": [
                {
                    "event_type": "dataset_sealed",
                    "attempt_id": "attempt-source",
                    "dataset_id": "dataset-source",
                }
            ]
        },
        "successor": {
            "transition": {"state": "failed"},
            "commit": None,
            "artifacts": [],
        },
    }


class _CoreClient:
    def __init__(self, values: dict) -> None:
        self.values = values

    def readiness(self):
        return {"service_reachable": True}

    def json(self, _method, path, **_kwargs):
        if path.startswith("/v2/internal/training-attempts/"):
            return self.values["attempt"]
        if path.startswith("/v2/tasks/") and path.endswith("timeline?limit=100"):
            return self.values["timeline"]
        if path.startswith("/v2/tasks/"):
            return self.values["task"]
        if path.startswith("/v2/projects/"):
            return self.values["project"]
        if path.startswith("/v2/internal/training-feedback/datasets/"):
            if self.values.get("dataset_missing"):
                raise CoreControlError("missing dataset")
            return self.values["dataset"]
        if path.startswith("/v2/internal/training-successors/"):
            return self.values["successor"]
        raise AssertionError(path)

    def bytes(self, _path):
        return self.values["archive"], {}

    def close(self):
        pass


def _port_request(config: ExperimentConfig) -> dict:
    source = config.experiment_root / "supervisor" / SOURCE / "runs/Astronomy_004_a0_v2"
    source.mkdir(parents=True, exist_ok=True)
    if not (source / "INSTRUCTIONS.md").exists():
        (source / "INSTRUCTIONS.md").write_text(
            "public instructions", encoding="utf-8"
        )
        (source / "_meta.json").write_text("{}", encoding="utf-8")
        (source / "data").mkdir()
        (source / "data/input.csv").write_text("x\n1\n", encoding="utf-8")
        (source / "related_work").mkdir()
        (source / "related_work/paper.pdf").write_bytes(b"pdf")
    return {
        "task_id": "Astronomy_004",
        "source_namespace": SOURCE,
        "source_state_sha256": "a" * 64,
        "source_task_id": "Astronomy_004",
        "source_attempt_index": 0,
        "source_run_id": "Astronomy_004_a0_v2",
        "source_candidate_output_root": str(source),
        "source_core_project_id": "project-source",
        "source_core_task_id": "task-source",
        "source_core_attempt_id": "attempt-source",
        "source_session_id": "session-source",
        "source_dataset_id": "dataset-source",
        "source_dataset_revision": "dataset-revision.v1",
        "source_session_result_sha256": "6" * 64,
        "source_adapter_identity": "3" * 64,
        "source_protocol_identity": "1" * 64,
        "source_core_identity": "2" * 64,
        "expected_model": "gpt-5.5",
        "expected_harness": "codex",
        "expected_capture_mode": "transcript",
        "expected_codex_version": "0.144.1",
        "migration_executor_adapter_identity": "3" * 64,
        "migration_executor_core_identity": "2" * 64,
        "migration_protocol_identity": "b" * 64,
        "migration_core_generation": "4" * 32,
        "migration_release_identity": "5" * 64,
        "fresh_workspace": True,
        "candidate_reexecuted": False,
        "additional_candidate_model_calls": 0,
    }


def test_core_reconciliation_port_materializes_exact_sealed_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    values = _core_fixture()
    monkeypatch.setattr(ports_module, "CoreControlV2Client", lambda _authority: _CoreClient(values))
    port = CoreV2CandidateReconciliationPort(
        config,
        config.experiment_root / "supervisor" / TARGET,
        core_authority=_authority(),
    )
    result = port.execute(_port_request(config), "reconciliation-port-once")
    assert result["candidate_reexecuted"] is False
    assert result["additional_candidate_model_calls"] == 0
    assert result["seal_origin"] == "reconciliation"
    assert Path(result["candidate_output_root"], "report/report.md").is_file()
    assert port.recover(_port_request(config), "reconciliation-port-once") == result


@pytest.mark.parametrize(
    "mutation",
    [
        "session_missing",
        "session_unsealed",
        "dataset_missing",
        "dataset_duplicate",
        "session_hash",
        "dataset_revision",
        "task_id",
        "attempt_id",
        "model",
        "successor_pending",
    ],
)
def test_core_reconciliation_port_fails_closed_on_authority_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    config = _config(tmp_path)
    values = _core_fixture()
    request = _port_request(config)
    if mutation == "session_missing":
        values["dataset"]["session_id"] = "other-session"
    elif mutation == "session_unsealed":
        values["attempt"]["session_result"]["status"] = "RUNNING"
    elif mutation == "dataset_missing":
        values["dataset_missing"] = True
    elif mutation == "dataset_duplicate":
        values["timeline"]["items"].append(deepcopy(values["timeline"]["items"][0]))
    elif mutation == "session_hash":
        values["attempt"]["execution_receipt"]["session_result_sha256"] = "0" * 64
    elif mutation == "dataset_revision":
        values["dataset"]["completed_dataset_revision"] = "other.v1"
    elif mutation == "task_id":
        values["project"]["config"]["task"]["title"] = "Life_005"
    elif mutation == "attempt_id":
        values["task"]["authoritative_attempt_id"] = "other-attempt"
    elif mutation == "model":
        values["attempt"]["execution_receipt"]["model_ref"] = "other-model"
    else:
        values["successor"]["transition"]["state"] = "running"
    monkeypatch.setattr(ports_module, "CoreControlV2Client", lambda _authority: _CoreClient(values))
    port = CoreV2CandidateReconciliationPort(
        config,
        config.experiment_root / "supervisor" / TARGET,
        core_authority=_authority(),
    )
    with pytest.raises((ValueError, CoreControlError)):
        port.execute(request, f"reconciliation-reject-{mutation}")
