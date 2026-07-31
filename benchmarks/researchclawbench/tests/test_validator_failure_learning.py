from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest
from openevo_researchclawbench.config import ExperimentConfig
from openevo_researchclawbench.production_operation_ports import (
    CoreFeedbackPort,
    build_validator_feedback_layers,
)
from openevo_researchclawbench.training_state_store import TrainingStateStore
from openevo_researchclawbench.training_supervisor import (
    CommunityTrainingSupervisor,
    SupervisorIdentity,
    _select_best,
)
from openevo_researchclawbench.transition_engine import TrainingStage
from openevo_researchclawbench.validator_failure_learning import (
    ValidatorFailureLearningSpec,
    ValidatorLearningPreEffectReplacementSpec,
    initialize_validator_failure_learning,
    replace_validator_learning_post_feedback_pre_evolution_namespace,
    replace_validator_learning_pre_effect_namespace,
)

IDENTITY = SupervisorIdentity("1" * 64, "2" * 64, "3" * 64)
POLICY = {
    "enabled": True,
    "consumes_attempt": True,
    "candidate_retry_same_attempt": False,
    "judge_on_validator_failure": False,
    "reflector_on_validator_failure": True,
    "feedback_source": [
        "candidate_trace",
        "candidate_report",
        "validator_receipt",
    ],
    "official_test_enabled": False,
    "run_id_suffix": "v9",
}


def _candidate(attempt: int, *, source: bool = False) -> dict:
    return {
        "session_id": f"session-{attempt}",
        "dataset_id": f"dataset-{attempt}",
        "dataset_revision": f"dataset-{attempt}.v1",
        "completed": True,
        "run_id": "Astronomy_004_a0_v2" if source else f"Astronomy_004_a{attempt}_v9",
        "core_project_id": "project-synthetic",
        "core_task_id": f"task-{attempt}",
        "core_attempt_id": f"attempt-{attempt}",
        "workspace_binding_id": f"workspace-{attempt}",
        "task_request_id": f"request-{attempt}",
        "session_result_id": "a" * 64,
        "successor_transition_id": f"successor-{attempt}",
        "transcript_receipt": {"sha256": "b" * 64},
        "successor_workspace_authority": None,
        "runtime_seconds": float(20 + attempt),
        "cost_total_usd": None,
        "composite_size_bytes": 100 + attempt,
        "candidate_reexecuted": False,
        "additional_candidate_model_calls": 0,
    }


def _invalid() -> dict:
    return {
        "artifact_valid": False,
        "completeness": 15,
        "artifact_root_sha256": None,
        "validator_errors": ["REPORT_REQUIRED_SECTIONS_MISSING"],
        "content_sha256": "c" * 64,
    }


class ValidatorLearningOperations:
    def __init__(self, valid_attempts: set[int] = frozenset(), scores=None) -> None:
        self.valid_attempts = set(valid_attempts)
        self.scores = scores or {1: 0.4, 2: 0.6}
        self.calls: dict[str, int] = {}
        self.results: dict[str, dict] = {}

    def _once(self, key: str, value: dict) -> dict:
        if key in self.results:
            assert self.results[key] == value
            return value
        self.calls[key] = self.calls.get(key, 0) + 1
        self.results[key] = value
        return value

    def candidate_readiness(self, request):
        del request
        return {
            "service_reachable": True,
            "bearer_present": True,
            "bearer_valid": True,
            "generation_matches": True,
            "candidate_port_authorized": True,
            "secret_recorded": False,
            "generation": "d" * 32,
            "release_identity": "e" * 64,
        }

    def evolution_readiness(self, request):
        del request
        return {
            "ready": True,
            "identity_matches": True,
            "authority_issued": True,
            "docker_mount_created": True,
            "container_path_visible": True,
            "container_user_can_read": True,
            "generation_matches": True,
            "release_identity_matches": True,
            "adoption_receipt_valid": True,
            "cleanup_verified": True,
            "secret_recorded": False,
            "codex_cli_started": False,
            "model_started": False,
        }

    def ensure_candidate(self, request, idempotency_key):
        return self._once(idempotency_key, _candidate(request["attempt_index"]))

    def validate_artifact(self, request, idempotency_key):
        attempt = int(request["candidate"]["run_id"].split("_a", 1)[1].split("_", 1)[0])
        result = (
            {
                "artifact_valid": True,
                "completeness": 19,
                "artifact_root_sha256": str(attempt) * 64,
                "validator_errors": [],
            }
            if attempt in self.valid_attempts
            else _invalid()
        )
        return self._once(idempotency_key, result)

    def evaluate(self, request, idempotency_key):
        attempt = int(request["candidate"]["run_id"].split("_a", 1)[1].split("_", 1)[0])
        return self._once(idempotency_key, {"total_score": self.scores[attempt]})

    def attach_feedback(self, request, idempotency_key):
        attempt = request["attempt_index"]
        source = request.get("feedback_source", "community_evaluator")
        if source == "artifact_validator":
            assert request["validation"]["artifact_valid"] is False
        return self._once(
            idempotency_key,
            {
                "attachment_id": f"attachment-{attempt}",
                "attachment_sha256": str(7 + attempt) * 64,
                "resolved_view_sha256": str(4 + attempt) * 64,
                "task_local_overlay_id": f"overlay-{attempt}",
                "task_local_overlay_scope_id": f"task-{attempt}",
                "successor_transition_id": f"successor-{attempt}",
                "feedback_class": "MIXED" if source == "artifact_validator" else "SOFT_JUDGE",
                "feedback_source": source,
                "judge_calls": 0 if source == "artifact_validator" else 1,
            },
        )

    def evolve_artifacts(self, request, idempotency_key):
        attempt = request["attempt_index"]
        return self._once(
            idempotency_key,
            {
                "jobs": [
                    {
                        "artifact_type": kind,
                        "job_id": f"job-{attempt}-{kind}",
                        "action": "update",
                    }
                    for kind in ("agent_system", "text_memory", "skill_bundle")
                ]
            },
        )

    def admit_composite(self, request, idempotency_key):
        attempt = request["attempt_index"]
        return self._once(
            idempotency_key,
            {
                "composite_id": f"composite-{attempt + 1}",
                "registry_artifact_ids": [f"artifact-{attempt}-{kind}" for kind in range(3)],
            },
        )

    def destroy_task_local(self, request, idempotency_key):
        return self._once(idempotency_key, {"destroyed": True, "overlay_id": request.get("overlay_id")})

    def sanitize_composite(self, request, idempotency_key):
        return self._once(idempotency_key, {"passed": True, "composite_id": request["composite_id"]})


def _supervisor(tmp_path: Path, operations: ValidatorLearningOperations):
    return CommunityTrainingSupervisor(
        store=TrainingStateStore(tmp_path / "state"),
        experiment_id="rcb_oe_v0_astronomy004_validator_learning_v9_test",
        identity=IDENTITY,
        operations=operations,
        task_ids=("Astronomy_004",),
        validator_failure_policy=POLICY,
    )


def _initialize(supervisor: CommunityTrainingSupervisor) -> None:
    supervisor.initialize_validator_failure_learning(
        source_reference={
            "task_id": "Astronomy_004",
            "attempt_index": 0,
            "candidate_reexecuted": False,
            "additional_candidate_model_calls": 0,
        },
        candidate=_candidate(0, source=True),
        validation=_invalid(),
        source_receipt_sha256="f" * 64,
    )


def _drive(supervisor: CommunityTrainingSupervisor, target: str) -> dict:
    for _ in range(100):
        state = supervisor.status()
        if state["stage"] == target:
            return state
        supervisor.run_next()
    raise AssertionError(f"did not reach {target}")


def test_v9_protocol_is_community_only_and_official_policy_is_terminal() -> None:
    protocol = Path("/home/lhy-h/work/researchclaw_openevo/experiments/sequential_task_reflector_evolution_v0/protocol/community_validator_learning_v9.yaml")
    config = ExperimentConfig.load(protocol)
    assert config.community_validator_failure_policy == POLICY
    assert config.require("official_validator_failure_policy") == {
        "enabled": False,
        "fail_closed": True,
        "reflector_allowed": False,
        "evolution_allowed": False,
    }


def test_a0_invalid_source_is_consumed_without_candidate_or_judge(tmp_path: Path) -> None:
    operations = ValidatorLearningOperations()
    supervisor = _supervisor(tmp_path, operations)
    _initialize(supervisor)
    state = supervisor.status()
    records = supervisor.store.attempts_for_task(supervisor.experiment_id, "Astronomy_004")
    assert state["stage"] == "VALIDATOR_FEEDBACK_PENDING"
    assert state["attempt_status"] == "CONSUMED_INVALID_ARTIFACT"
    assert records[0]["score"] is None
    assert records[0]["evaluator_status"] == "SKIPPED"
    assert not any(key.endswith(":candidate") for key in operations.calls)
    assert not any(key.endswith(":evaluation") for key in operations.calls)


def test_invalid_a0_and_a1_each_create_feedback_and_triple_evolution(tmp_path: Path) -> None:
    operations = ValidatorLearningOperations(valid_attempts={2})
    supervisor = _supervisor(tmp_path, operations)
    _initialize(supervisor)
    _drive(supervisor, "TASK_SELECTION_PENDING")
    assert sum(key.endswith(":feedback-attachment") for key in operations.calls) == 2
    assert sum(key.endswith(":evolution") for key in operations.calls) == 2
    assert len(supervisor.status()["evolution_job_ids"]) == 6
    assert not any(":a0:candidate" in key for key in operations.calls)
    assert sum(key.endswith(":candidate") for key in operations.calls) == 2
    assert sum(key.endswith(":evaluation") for key in operations.calls) == 1


def test_best_of_three_prefers_valid_scored_and_destroys_overlay(tmp_path: Path) -> None:
    operations = ValidatorLearningOperations(valid_attempts={1, 2}, scores={1: 0.7, 2: 0.5})
    supervisor = _supervisor(tmp_path, operations)
    _initialize(supervisor)
    selected = _drive(supervisor, "TASK_BEST_SELECTED")
    assert selected["task_best"]["Astronomy_004"]["attempt_index"] == 1
    destroyed = supervisor.run_next()
    assert destroyed["stage"] == "TASK_LOCAL_DESTROYED"
    assert destroyed["current_task_local_overlay_id"] is None
    sanitized = supervisor.run_next()
    assert sanitized["stage"] == "CROSS_TASK_SANITIZED"


def test_three_invalid_attempts_end_task_without_judge_or_same_attempt_retry(tmp_path: Path) -> None:
    operations = ValidatorLearningOperations()
    supervisor = _supervisor(tmp_path, operations)
    _initialize(supervisor)
    terminal = _drive(supervisor, "TASK_NO_VALID_ATTEMPT")
    assert terminal["failure_reason"] == "TASK_NO_VALID_ATTEMPT"
    assert len(supervisor.store.attempts_for_task(supervisor.experiment_id, "Astronomy_004")) == 3
    assert sum(key.endswith(":candidate") for key in operations.calls) == 2
    assert sum(key.endswith(":evaluation") for key in operations.calls) == 0
    assert sum(key.endswith(":evolution") for key in operations.calls) == 2
    assert terminal["current_task_local_overlay_id"] is None


def test_multi_task_training_keeps_latest_evolved_composite_after_all_invalid(
    tmp_path: Path,
) -> None:
    operations = ValidatorLearningOperations()
    supervisor = CommunityTrainingSupervisor(
        store=TrainingStateStore(tmp_path / "multi-task-state"),
        experiment_id="rcb_oe_v0_multi_task_validator_learning_v9_test",
        identity=IDENTITY,
        operations=operations,
        task_ids=("Astronomy_004", "Chemistry_004"),
        validator_failure_policy=POLICY,
    )
    supervisor.initialize()

    closed = _drive(supervisor, "CROSS_TASK_SANITIZED")

    selected = closed["task_best"]["Astronomy_004"]
    assert selected["attempt_index"] == 2
    assert selected["input_composite_id"] == "composite-2"
    assert selected["selection_status"] == "NO_VALID_ARTIFACT_LATEST_EVOLVED"
    assert selected["artifact_valid"] is False
    assert closed["failure_reason"] is None
    assert closed["current_task_local_overlay_id"] is None
    assert not any(key.endswith(":evaluation") for key in operations.calls)


def test_restart_reuses_validator_attachment_candidate_and_evolution_receipts(tmp_path: Path) -> None:
    operations = ValidatorLearningOperations(valid_attempts={2})
    supervisor = _supervisor(tmp_path, operations)
    _initialize(supervisor)
    for _ in range(18):
        supervisor.run_next()
        supervisor = _supervisor(tmp_path, operations)
    assert all(count == 1 for count in operations.calls.values())


def test_best_of_three_ranking_groups_are_deterministic() -> None:
    invalid = {
        "attempt_index": 0,
        "completed": True,
        "artifact_valid": False,
        "score": None,
        "input_composite_id": "c000",
    }
    unscored = {
        "attempt_index": 1,
        "completed": True,
        "artifact_valid": True,
        "score": None,
        "validator_completeness": 20,
        "runtime_seconds": 12,
        "input_composite_id": "c001",
    }
    scored = {
        "attempt_index": 2,
        "completed": True,
        "artifact_valid": True,
        "score": 0.1,
        "validator_completeness": 10,
        "runtime_seconds": 30,
        "input_composite_id": "c002",
    }
    assert _select_best([invalid, unscored, scored])["attempt_index"] == 2
    assert _select_best([invalid, unscored, {**invalid, "attempt_index": 2}])[
        "attempt_index"
    ] == 1


def test_validator_feedback_layers_separate_global_and_task_local(tmp_path: Path) -> None:
    root = tmp_path / "run"
    (root / "report").mkdir(parents=True)
    (root / "report" / "report.md").write_text("# Summary\n## Findings\n", encoding="utf-8")
    candidate = {**_candidate(0), "candidate_output_root": str(root)}
    global_feedback, task_local = build_validator_feedback_layers(candidate, _invalid())
    assert "Astronomy_004" not in str(global_feedback)
    assert global_feedback["artifact_valid"] is False
    assert task_local["feedback_source"] == "artifact_validator"
    assert task_local["report_headings"] == ["Summary", "Findings"]


def test_feedback_port_projects_supervisor_audit_key_into_core_identifier() -> None:
    audit_key = (
        "rcb_oe_v0_astronomy004_validator_learning_v9:"
        "Astronomy_004:a0:feedback-attachment"
    )
    projected = CoreFeedbackPort._core_idempotency_key(audit_key)
    assert projected.startswith("rcb-feedback-")
    assert len(projected) == len("rcb-feedback-") + 64
    assert ":" not in projected
    assert projected == CoreFeedbackPort._core_idempotency_key(audit_key)
    assert projected != CoreFeedbackPort._core_idempotency_key(audit_key + "-other")
    assert '"official_mode"' not in inspect.getsource(CoreFeedbackPort.execute)


@pytest.mark.parametrize(
    "bad_tag",
    ["Astronomy_004", "raw_judge_reasoning", "target_paper", "custom answer 12.345"],
)
def test_task_specific_or_private_validator_tags_are_rejected(
    tmp_path: Path, bad_tag: str
) -> None:
    root = tmp_path / "run"
    (root / "report").mkdir(parents=True)
    (root / "report" / "report.md").write_text("# Report\n", encoding="utf-8")
    candidate = {**_candidate(0), "candidate_output_root": str(root)}
    with pytest.raises(ValueError, match="non-allowlisted"):
        build_validator_feedback_layers(
            candidate, {**_invalid(), "validator_errors": [bad_tag]}
        )


def test_validator_learning_cannot_initialize_without_explicit_policy(tmp_path: Path) -> None:
    supervisor = CommunityTrainingSupervisor(
        store=TrainingStateStore(tmp_path / "state"),
        experiment_id="rcb_oe_v0_astronomy004_validator_learning_v9_test",
        identity=IDENTITY,
        operations=ValidatorLearningOperations(),
        task_ids=("Astronomy_004",),
    )
    with pytest.raises(ValueError, match="requires one Community task"):
        _initialize(supervisor)


def _source_fixture(tmp_path: Path):
    experiment = tmp_path / "experiment"
    supervisor_root = experiment / "supervisor"
    v7 = "rcb_oe_v0_source_v7"
    v8 = "rcb_oe_v0_source_v8"
    target = "rcb_oe_v0_astronomy004_validator_learning_v9_test"
    candidate = _candidate(0, source=True)
    candidate["candidate_output_root"] = str(
        supervisor_root / v8 / "runs" / candidate["run_id"]
    )
    v7_store = TrainingStateStore(supervisor_root / v7)
    v7_store.initialize_experiment(
        experiment_id=v7,
        protocol_sha256="4" * 64,
        core_identity_sha256="2" * 64,
        adapter_identity_sha256="5" * 64,
        initial_state={"task_ids": ["Astronomy_004"], "failure_reason": None},
    )
    v7_store.transition(
        experiment_id=v7,
        idempotency_key="v7-ready",
        source=TrainingStage.INITIALIZED,
        target=TrainingStage.TASK_ATTEMPT_READY,
        updates={},
        receipt={},
    )
    v7_store.transition(
        experiment_id=v7,
        idempotency_key="v7-blocked",
        source=TrainingStage.TASK_ATTEMPT_READY,
        target=TrainingStage.BLOCKED,
        updates={"failure_reason": "CANDIDATE_OPERATION_FAILED_BEFORE_SEAL"},
        receipt={},
    )
    (supervisor_root / v7 / "evidence.json").write_text("{}\n", encoding="utf-8")

    v8_store = TrainingStateStore(supervisor_root / v8)
    v8_store.initialize_experiment(
        experiment_id=v8,
        protocol_sha256="6" * 64,
        core_identity_sha256="2" * 64,
        adapter_identity_sha256="7" * 64,
        initial_state={
            "task_ids": ["Astronomy_004"],
            "current_task_index": 0,
            "current_attempt": 0,
            "active_candidate_receipt": candidate,
            "failure_reason": None,
        },
    )
    v8_store.transition(
        experiment_id=v8,
        idempotency_key="v8-reconciled",
        source=TrainingStage.INITIALIZED,
        target=TrainingStage.CANDIDATE_SEALED_RECONCILED,
        updates={},
        receipt={},
    )
    v8_store.transition(
        experiment_id=v8,
        idempotency_key="v8-sealed",
        source=TrainingStage.CANDIDATE_SEALED_RECONCILED,
        target=TrainingStage.CANDIDATE_SEALED,
        updates={},
        receipt={},
    )
    validation = _invalid()
    v8_store.plan_side_effect(
        experiment_id=v8,
        idempotency_key="v8:candidate-reconciliation",
        kind="candidate-reconciliation",
        request={},
    )
    v8_store.complete_side_effect(
        idempotency_key="v8:candidate-reconciliation", receipt=candidate
    )
    v8_store.plan_side_effect(
        experiment_id=v8,
        idempotency_key="v8:validator",
        kind="validator",
        request={},
    )
    v8_store.complete_side_effect(idempotency_key="v8:validator", receipt=validation)
    v8_store.transition(
        experiment_id=v8,
        idempotency_key="v8-failed",
        source=TrainingStage.CANDIDATE_SEALED,
        target=TrainingStage.FAILED,
        updates={"failure_reason": "ARTIFACT_VALIDATOR_FAILED"},
        receipt=validation,
    )
    # Production source namespaces are quiescent before reconciliation. Mirror
    # that condition so the immutable read deliberately ignores no live WAL.
    for namespace in (v7, v8):
        database = supervisor_root / namespace / "training-supervisor.sqlite3"
        with __import__("sqlite3").connect(database) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    run = Path(candidate["candidate_output_root"])
    (run / "report").mkdir(parents=True)
    validator = run / "validator.json"
    validator.write_text(
        __import__("json").dumps(
            {key: validation[key] for key in ("artifact_valid", "completeness", "artifact_root_sha256", "validator_errors")},
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    protocol = tmp_path / "protocol.yaml"
    protocol.write_text("test: true\n", encoding="utf-8")
    config = SimpleNamespace(
        experiment_root=experiment,
        path=protocol,
        community_validator_failure_policy=POLICY,
        require=lambda key: {
            "source_identity.openevo_core_source_tree_sha256": "2" * 64,
            "source_identity.adapter_tree_sha256": "3" * 64,
        }[key],
    )
    v7_state = v7_store.load(v7)
    v8_state = v8_store.load(v8)
    spec = ValidatorFailureLearningSpec(
        operation_id="validator-learning-test-v1",
        source_candidate_namespace=v7,
        source_validator_namespace=v8,
        target_namespace=target,
        task_id="Astronomy_004",
        attempt_index=0,
        source_candidate_state_sha256=v7_state["_state_sha256"],
        source_validator_state_sha256=v8_state["_state_sha256"],
        source_session_id=candidate["session_id"],
        source_dataset_id=candidate["dataset_id"],
        source_dataset_revision=candidate["dataset_revision"],
        source_session_result_sha256=candidate["session_result_id"],
        source_validator_receipt_path=str(validator),
        source_validator_file_sha256=__import__("hashlib").sha256(validator.read_bytes()).hexdigest(),
        source_validator_operation_sha256=validation["content_sha256"],
        source_candidate_adapter_identity="5" * 64,
        source_candidate_protocol_identity="4" * 64,
        source_validator_adapter_identity="7" * 64,
        source_validator_protocol_identity="6" * 64,
        expected_model="gpt-5.5",
        expected_harness="codex",
        expected_capture_mode="transcript",
        expected_codex_version="0.144.1",
    )
    return config, spec, candidate, v7_store, v8_store


class _FakeCoreClient:
    def __init__(self, authority):
        del authority

    def close(self):
        pass

    def json(self, method, path):
        assert method == "GET"
        if path.startswith("/v2/internal/training-attempts/"):
            return {
                "execution_receipt": {
                    "session_id": "session-0",
                    "terminal_status": "COMPLETED",
                    "session_result_sha256": "a" * 64,
                    "model_ref": "gpt-5.5",
                    "harness_id": "codex",
                    "capture_mode": "transcript",
                },
                "session_result": {
                    "session_id": "session-0",
                    "status": "COMPLETED",
                    "metadata": {
                        "agent": {"model_name": "gpt-5.5"},
                        "openevo": {"credential_isolation": {"codex_version": "0.144.1"}},
                    },
                },
            }
        if path.startswith("/v2/tasks/"):
            return {"authoritative_attempt_id": "attempt-0"}
        if path.startswith("/v2/internal/training-feedback/datasets/"):
            return {
                "session_id": "session-0",
                "completed_dataset_id": "dataset-0",
                "completed_dataset_revision": "dataset-0.v1",
            }
        if "/v2/internal/training-feedback/sessions/" in path:
            return {"session_id": "session-0", "attachments": []}
        if path.startswith("/v2/projects/"):
            return {
                "active_project_head": {
                    "project_head_id": "head-0",
                    "manifest_sha256": "8" * 64,
                    "evolution_revision": {"artifact_count": 0},
                }
            }
        if path.startswith("/v2/internal/training-successors/"):
            return {"transition": {"state": "failed"}, "artifacts": []}
        raise AssertionError(path)


class _FakeComposite:
    def register_baseline(self, *, project_id, project_head):
        assert project_id == "project-synthetic"
        assert project_head["project_head_id"] == "head-0"
        return {"composite_id": "c000", "composite_sha256": "9" * 64}


def test_append_only_source_reference_initializes_v9_without_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, spec, _, v7_store, v8_store = _source_fixture(tmp_path)
    before = (
        v7_store.load(spec.source_candidate_namespace)["_state_sha256"],
        v8_store.load(spec.source_validator_namespace)["_state_sha256"],
    )
    monkeypatch.setattr(
        "openevo_researchclawbench.validator_failure_learning.CoreControlV2Client",
        _FakeCoreClient,
    )
    monkeypatch.setattr(
        "openevo_researchclawbench.validator_failure_learning.build_production_ports",
        lambda *args, **kwargs: SimpleNamespace(composite=_FakeComposite()),
    )
    authority = SimpleNamespace(generation="g" * 32, release_identity="r" * 64)
    result = initialize_validator_failure_learning(
        config=config, spec=spec, core_authority=authority
    )
    state = TrainingStateStore(
        config.experiment_root / "supervisor" / spec.target_namespace
    ).load(spec.target_namespace)
    after = (
        v7_store.load(spec.source_candidate_namespace)["_state_sha256"],
        v8_store.load(spec.source_validator_namespace)["_state_sha256"],
    )
    assert result["migration"]["candidate_reexecuted"] is False
    assert state["stage"] == "VALIDATOR_FEEDBACK_PENDING"
    assert before == after
    assert state["additional_candidate_model_calls"] == 0


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("source_candidate_state_sha256", "0" * 64, "source supervisors"),
        ("source_validator_state_sha256", "0" * 64, "source supervisors"),
        ("source_session_id", "wrong-session", "candidate/validator authority"),
        ("source_dataset_revision", "wrong.v1", "candidate/validator authority"),
        ("expected_model", "other-model", "Core authority failed"),
    ],
)
def test_source_reference_rejects_identity_or_authority_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
    error: str,
) -> None:
    config, spec, _, _, _ = _source_fixture(tmp_path)
    payload = {**spec.__dict__, field: value}
    changed = ValidatorFailureLearningSpec(**payload)
    monkeypatch.setattr(
        "openevo_researchclawbench.validator_failure_learning.CoreControlV2Client",
        _FakeCoreClient,
    )
    monkeypatch.setattr(
        "openevo_researchclawbench.validator_failure_learning.build_production_ports",
        lambda *args, **kwargs: SimpleNamespace(composite=_FakeComposite()),
    )
    with pytest.raises(ValueError, match=error):
        initialize_validator_failure_learning(
            config=config,
            spec=changed,
            core_authority=SimpleNamespace(generation="g" * 32, release_identity="r" * 64),
        )


def test_pre_effect_identity_replacement_is_append_only_and_model_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, source_spec, _, v7_store, v8_store = _source_fixture(tmp_path)
    monkeypatch.setattr(
        "openevo_researchclawbench.validator_failure_learning.CoreControlV2Client",
        _FakeCoreClient,
    )
    monkeypatch.setattr(
        "openevo_researchclawbench.validator_failure_learning.build_production_ports",
        lambda *args, **kwargs: SimpleNamespace(composite=_FakeComposite()),
    )
    authority = SimpleNamespace(generation="g" * 32, release_identity="r" * 64)
    initialize_validator_failure_learning(
        config=config, spec=source_spec, core_authority=authority
    )
    source_store = TrainingStateStore(
        config.experiment_root / "supervisor" / source_spec.target_namespace
    )
    source_store.plan_side_effect(
        experiment_id=source_spec.target_namespace,
        idempotency_key=f"{source_spec.target_namespace}:Astronomy_004:a0:feedback-attachment",
        kind="feedback-attachment",
        request={"feedback_source": "artifact_validator"},
    )
    database = (
        config.experiment_root
        / "supervisor"
        / source_spec.target_namespace
        / "training-supervisor.sqlite3"
    )
    with __import__("sqlite3").connect(database) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    source_state = source_store.load(source_spec.target_namespace)
    replacement = ValidatorLearningPreEffectReplacementSpec(
        operation_id="validator-learning-replacement-test-v1",
        source_namespace=source_spec.target_namespace,
        target_namespace="rcb_oe_v0_astronomy004_validator_learning_v9_replacement",
        source_state_sha256=source_state["_state_sha256"],
        source_protocol_identity=source_state["_protocol_sha256"],
        source_core_identity=source_state["_core_identity_sha256"],
        source_adapter_identity=source_state["_adapter_identity_sha256"],
    )
    result = replace_validator_learning_pre_effect_namespace(
        config=config, spec=replacement, core_authority=authority
    )
    target = TrainingStateStore(
        config.experiment_root / "supervisor" / replacement.target_namespace
    ).load(replacement.target_namespace)
    assert result["replacement"]["candidate_reexecuted"] is False
    assert result["source"]["completed_external_effects"] == 0
    assert target["stage"] == "VALIDATOR_FEEDBACK_PENDING"
    assert target["additional_candidate_model_calls"] == 0
    assert v7_store.load(source_spec.source_candidate_namespace)["stage"] == "BLOCKED"
    assert v8_store.load(source_spec.source_validator_namespace)["stage"] == "FAILED"


class _PostFeedbackCoreClient(_FakeCoreClient):
    attachment_id = "attachment-0"

    def json(self, method, path):
        assert method == "GET"
        if "/v2/internal/training-feedback/sessions/" in path:
            return {
                "session_id": "session-0",
                "attachments": [
                    {
                        "attachment_id": self.attachment_id,
                        "status": "sealed",
                        "session_id": "session-0",
                        "dataset_id": "dataset-0",
                        "dataset_revision": "dataset-0.v1",
                        "task_id": "rollout-attempt-0",
                        # Historical defect: public benchmark identity was
                        # substituted for the Core science-task scope.
                        "task_scope_id": "Astronomy_004",
                    }
                ],
            }
        if path.startswith("/v2/internal/training-feedback/datasets/"):
            return {
                "session_id": "session-0",
                "completed_dataset_id": "dataset-0",
                "completed_dataset_revision": "dataset-0.v1",
                "task_id": "rollout-attempt-0",
            }
        if path.startswith("/v2/internal/training-successors/"):
            transition_id = path.rsplit("/", 1)[-1]
            return {
                "transition": {
                    "state": "failed",
                    "transition": {
                        "successor_transition_id": transition_id,
                    },
                },
                "attempts": [
                    {"ordinal": 1, "state": "failed"},
                    {"ordinal": 2, "state": "failed"},
                ],
                "commit": None,
                "artifacts": [],
            }
        return super().json(method, path)


def _post_feedback_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[
    object,
    ValidatorLearningPreEffectReplacementSpec,
    str,
    TrainingStateStore,
]:
    config, source_spec, _, _, _ = _source_fixture(tmp_path)
    monkeypatch.setattr(
        "openevo_researchclawbench.validator_failure_learning.CoreControlV2Client",
        _FakeCoreClient,
    )
    monkeypatch.setattr(
        "openevo_researchclawbench.validator_failure_learning.build_production_ports",
        lambda *args, **kwargs: SimpleNamespace(composite=_FakeComposite()),
    )
    authority = SimpleNamespace(generation="g" * 32, release_identity="r" * 64)
    initialize_validator_failure_learning(
        config=config, spec=source_spec, core_authority=authority
    )
    source_store = TrainingStateStore(
        config.experiment_root / "supervisor" / source_spec.target_namespace
    )
    supervisor = CommunityTrainingSupervisor(
        store=source_store,
        experiment_id=source_spec.target_namespace,
        identity=SupervisorIdentity(
            protocol_sha256=source_store.load(source_spec.target_namespace)[
                "_protocol_sha256"
            ],
            core_identity_sha256="2" * 64,
            adapter_identity_sha256="3" * 64,
        ),
        operations=ValidatorLearningOperations(),
        task_ids=("Astronomy_004",),
        validator_failure_policy=POLICY,
    )
    assert supervisor.run_next()["stage"] == "ATTACHMENT_SEALED"
    assert supervisor.run_next()["stage"] == "EVOLUTION_PENDING"
    state = supervisor.run_next()
    assert state["stage"] == "EVOLUTION_RUNNING"
    database = (
        config.experiment_root
        / "supervisor"
        / source_spec.target_namespace
        / "training-supervisor.sqlite3"
    )
    with __import__("sqlite3").connect(database) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    replacement = ValidatorLearningPreEffectReplacementSpec(
        operation_id="validator-learning-post-feedback-test-v1",
        source_namespace=source_spec.target_namespace,
        target_namespace=(
            "rcb_oe_v0_astronomy004_validator_learning_v9_post_feedback"
        ),
        source_state_sha256=state["_state_sha256"],
        source_protocol_identity=state["_protocol_sha256"],
        source_core_identity=state["_core_identity_sha256"],
        source_adapter_identity=state["_adapter_identity_sha256"],
    )
    return config, replacement, source_spec.target_namespace, source_store


def test_post_feedback_replacement_is_append_only_model_free_and_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, spec, source_namespace, source_store = _post_feedback_source(
        tmp_path, monkeypatch
    )
    source_before = source_store.load(source_namespace)["_state_sha256"]
    monkeypatch.setattr(
        "openevo_researchclawbench.validator_failure_learning.CoreControlV2Client",
        _PostFeedbackCoreClient,
    )
    monkeypatch.setattr(
        "openevo_researchclawbench.validator_failure_learning.build_production_ports",
        lambda *args, **kwargs: SimpleNamespace(composite=_FakeComposite()),
    )
    authority = SimpleNamespace(generation="g" * 32, release_identity="r" * 64)
    first = replace_validator_learning_post_feedback_pre_evolution_namespace(
        config=config, spec=spec, core_authority=authority
    )
    second = replace_validator_learning_post_feedback_pre_evolution_namespace(
        config=config, spec=spec, core_authority=authority
    )
    target = TrainingStateStore(
        config.experiment_root / "supervisor" / spec.target_namespace
    ).load(spec.target_namespace)
    assert first == second
    assert first["source"]["completed_evolution_jobs"] == 0
    assert first["replacement"]["candidate_reexecuted"] is False
    assert target["stage"] == "VALIDATOR_FEEDBACK_PENDING"
    assert target["attachment_ids"] == []
    assert target["evolution_job_ids"] == []
    assert target["additional_candidate_model_calls"] == 0
    assert source_store.load(source_namespace)["_state_sha256"] == source_before


def test_post_feedback_replacement_rejects_nonfailed_successor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, spec, _, _ = _post_feedback_source(tmp_path, monkeypatch)

    class CommittedCore(_PostFeedbackCoreClient):
        def json(self, method, path):
            result = super().json(method, path)
            if path.startswith("/v2/internal/training-successors/"):
                result["transition"]["state"] = "committed"
            return result

    monkeypatch.setattr(
        "openevo_researchclawbench.validator_failure_learning.CoreControlV2Client",
        CommittedCore,
    )
    with pytest.raises(ValueError, match="Core authority is not closed"):
        replace_validator_learning_post_feedback_pre_evolution_namespace(
            config=config,
            spec=spec,
            core_authority=SimpleNamespace(
                generation="g" * 32, release_identity="r" * 64
            ),
        )
