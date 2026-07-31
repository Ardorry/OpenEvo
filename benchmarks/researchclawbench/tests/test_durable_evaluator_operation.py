from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import openevo_researchclawbench.community_evaluator as evaluator_module
import pytest
from openevo_researchclawbench.community_evaluator import (
    CommunityEvaluatorExecution,
    DurableCommunityEvaluatorPort,
    DurableCommunityJudgeExecutor,
    EvaluatorError,
    ProductionCommunityEvaluatorBundle,
    build_production_community_evaluator,
    run_community_evaluator_detailed,
    scorer_tracked_tree_sha256,
)
from openevo_researchclawbench.durable_evaluator_operation import (
    OPENROUTER_API_BASE,
    UNAVAILABLE,
    AmbiguousJudgeInvocation,
    DurableEvaluatorOperation,
    EvaluationIdentityConflict,
    EvaluationIntegrityError,
    EvaluationPhase,
    FeedbackClass,
    FeedbackPartition,
    JudgeConfigurationError,
    JudgeExecutionOutcome,
    JudgePolicyIdentity,
    JudgeRuntimeIdentity,
    redact_text,
)


def _policy(**updates) -> JudgePolicyIdentity:
    raw = {
        "model": "openai/gpt-5.1",
        "provider": "openai_compatible",
        "model_source": "OpenRouter_model_catalog",
        "api_base": OPENROUTER_API_BASE,
        "api_key_env": "JUDGE_API_KEY",
        "api_base_env": "JUDGE_API_BASE",
        "model_env": "JUDGE_MODEL_NAME",
        "runs_per_attempt": 1,
        "scorer_commit": "5" * 40,
    }
    raw.update(updates)
    return JudgePolicyIdentity.from_protocol(raw, scorer_sha256="6" * 64)


def _request(**updates) -> dict:
    raw = {
        "task_id": "Astronomy_004",
        "attempt_id": "Astronomy_004_a0_v11",
        "session_id": "session-fresh-v11",
        "completed_dataset_id": "dataset-fresh-v11",
        "dataset_revision": "art_fresh.v1",
        "validator_receipt_sha256": "7" * 64,
        "artifact_root_sha256": "8" * 64,
        "candidate_runtime_seconds": 121,
        "generic_failure_tags": [],
    }
    raw.update(updates)
    return raw


def _outcome(
    *,
    score: float = 73.5,
    request_count=UNAVAILABLE,
    usage=UNAVAILABLE,
    cost=UNAVAILABLE,
) -> JudgeExecutionOutcome:
    return JudgeExecutionOutcome(
        raw_score={
            "total_score": score,
            "items": [{"score": score, "reasoning": "private evaluator text"}],
        },
        runtime_identity=JudgeRuntimeIdentity(
            provider="openai_compatible",
            api_base=OPENROUTER_API_BASE,
            model="openai/gpt-5.1",
            api_key_present=True,
        ),
        request_count=request_count,
        usage=usage,
        cost_total_usd=cost,
    )


class CountingExecutor:
    def __init__(self, outcome: JudgeExecutionOutcome | None = None) -> None:
        self.calls = 0
        self.outcome = outcome or _outcome()

    def __call__(self, request, invocation):
        self.calls += 1
        assert request["session_id"] == "session-fresh-v11"
        assert invocation.model == "openai/gpt-5.1"
        assert invocation.provider == "openai_compatible"
        assert invocation.api_base == OPENROUTER_API_BASE
        return self.outcome


def test_durable_evaluator_exactly_once_and_restart_recovery(tmp_path: Path) -> None:
    executor = CountingExecutor()
    operation = DurableEvaluatorOperation(tmp_path / "evaluator", executor=executor)
    first = operation.execute(
        request=_request(), idempotency_key="formal-v11-evaluate-a0", policy=_policy()
    )
    restarted = DurableEvaluatorOperation(tmp_path / "evaluator", executor=executor)
    second = restarted.execute(
        request=_request(), idempotency_key="formal-v11-evaluate-a0", policy=_policy()
    )
    assert executor.calls == 1
    assert first.public_receipt_sha256 == second.public_receipt_sha256
    assert first.recovered is False
    assert second.recovered is True
    assert second.public_receipt["request_count"] == UNAVAILABLE
    assert second.public_receipt["usage"] == UNAVAILABLE
    assert second.public_receipt["cost_total_usd"] == UNAVAILABLE
    assert "reasoning" not in json.dumps(second.public_receipt).casefold()


def test_raw_private_and_public_authority_commit_atomically(tmp_path: Path) -> None:
    operation = DurableEvaluatorOperation(tmp_path / "evaluator", executor=CountingExecutor())
    result = operation.execute(
        request=_request(), idempotency_key="formal-v11-atomic", policy=_policy()
    )
    with sqlite3.connect(operation.store.db_path) as connection:
        row = connection.execute(
            "SELECT phase, raw_response_json, private_feedback_json, public_receipt_json, "
            "raw_response_sha256, private_feedback_sha256, public_receipt_sha256 "
            "FROM evaluator_operations WHERE idempotency_key = ?",
            ("formal-v11-atomic",),
        ).fetchone()
    assert row is not None
    assert row[0] == EvaluationPhase.SUCCEEDED.value
    assert all(value is not None for value in row[1:])
    assert result.raw_response_sha256 == row[4]
    assert result.private_feedback_sha256 == row[5]
    assert result.public_receipt_sha256 == row[6]


def test_response_lost_after_judge_never_repeats_billed_call(tmp_path: Path) -> None:
    executor = CountingExecutor()
    operation = DurableEvaluatorOperation(tmp_path / "evaluator", executor=executor)

    def crash_after_judge() -> None:
        raise RuntimeError("synthetic response loss")

    with pytest.raises(RuntimeError, match="synthetic response loss"):
        operation.execute(
            request=_request(),
            idempotency_key="formal-v11-lost-response",
            policy=_policy(),
            after_judge_hook=crash_after_judge,
        )
    assert executor.calls == 1
    assert operation.store.get("formal-v11-lost-response")["phase"] == (
        EvaluationPhase.FAILED_AMBIGUOUS.value
    )
    with pytest.raises(AmbiguousJudgeInvocation):
        DurableEvaluatorOperation(tmp_path / "evaluator", executor=executor).execute(
            request=_request(),
            idempotency_key="formal-v11-lost-response",
            policy=_policy(),
        )
    assert executor.calls == 1


@pytest.mark.parametrize(
    ("changed_request", "changed_policy"),
    [
        ({"session_id": "different-session"}, {}),
        ({}, {"model": "openai/gpt-5.2"}),
        ({}, {"provider": "different-provider"}),
        ({}, {"api_base": "https://different.invalid/v1"}),
        ({}, {"scorer_commit": "a" * 40}),
    ],
)
def test_idempotency_rejects_request_policy_model_provider_and_scorer_conflicts(
    tmp_path: Path, changed_request: dict, changed_policy: dict
) -> None:
    executor = CountingExecutor()
    operation = DurableEvaluatorOperation(tmp_path / "evaluator", executor=executor)
    operation.execute(request=_request(), idempotency_key="formal-v11-conflict", policy=_policy())
    if "provider" in changed_policy or "api_base" in changed_policy:
        # Invalid protocols fail even before the idempotency comparison.
        with pytest.raises(JudgeConfigurationError):
            policy = _policy(**changed_policy)
        assert executor.calls == 1
        return
    policy = _policy(**changed_policy)
    with pytest.raises(EvaluationIdentityConflict):
        operation.execute(
            request=_request(**changed_request),
            idempotency_key="formal-v11-conflict",
            policy=policy,
        )
    assert executor.calls == 1


def test_scorer_source_hash_conflict_is_rejected(tmp_path: Path) -> None:
    executor = CountingExecutor()
    operation = DurableEvaluatorOperation(tmp_path / "evaluator", executor=executor)
    operation.execute(request=_request(), idempotency_key="formal-v11-scorer", policy=_policy())
    changed = replace(_policy(), scorer_sha256="9" * 64)
    with pytest.raises(EvaluationIdentityConflict):
        operation.execute(
            request=_request(),
            idempotency_key="formal-v11-scorer",
            policy=changed,
        )
    assert executor.calls == 1


def test_openrouter_runtime_identity_requires_exact_model_and_base() -> None:
    policy = _policy()
    receipt = policy.validate_runtime_environment(
        {
            "JUDGE_API_KEY": "not-returned",
            "JUDGE_API_BASE": OPENROUTER_API_BASE,
            "JUDGE_MODEL_NAME": "openai/gpt-5.1",
        }
    )
    assert receipt == {
        "api_key_present": True,
        "api_base_present": True,
        "model_present": True,
        "api_base": OPENROUTER_API_BASE,
        "model": "openai/gpt-5.1",
        "provider": "openai_compatible",
        "model_slug_preserved": True,
        "secret_recorded": False,
    }
    with pytest.raises(JudgeConfigurationError, match="model differs"):
        policy.validate_runtime_environment(
            {
                "JUDGE_API_KEY": "not-returned",
                "JUDGE_API_BASE": OPENROUTER_API_BASE,
                "JUDGE_MODEL_NAME": "gpt-5.1",
            }
        )
    with pytest.raises(JudgeConfigurationError, match="base differs"):
        policy.validate_runtime_environment(
            {
                "JUDGE_API_KEY": "not-returned",
                "JUDGE_API_BASE": "https://different.invalid/v1",
                "JUDGE_MODEL_NAME": "openai/gpt-5.1",
            }
        )


def test_protocol_environment_variable_contract_is_closed() -> None:
    with pytest.raises(JudgeConfigurationError, match="variable contract"):
        _policy(model_env="UNTRUSTED_MODEL_NAME")


def test_metrics_are_measured_or_explicitly_unavailable(tmp_path: Path) -> None:
    measured = _outcome(
        request_count=7,
        usage={"prompt_tokens": 100, "completion_tokens": 20},
        cost=0.0123,
    )
    result = DurableEvaluatorOperation(
        tmp_path / "measured", executor=CountingExecutor(measured)
    ).execute(request=_request(), idempotency_key="formal-v11-measured", policy=_policy())
    assert result.public_receipt["request_count"] == 7
    assert result.public_receipt["usage"]["prompt_tokens"] == 100
    assert result.public_receipt["cost_total_usd"] == 0.0123
    with pytest.raises(EvaluationIntegrityError, match="request_count"):
        _outcome(request_count="estimated").validate(_policy())
    with pytest.raises(EvaluationIntegrityError, match="cost"):
        _outcome(cost=None).validate(_policy())


@pytest.mark.parametrize("feedback_class", list(FeedbackClass))
def test_feedback_classes_keep_private_teacher_data_task_local(
    tmp_path: Path, feedback_class: FeedbackClass
) -> None:
    def partitioner(_request, outcome):
        task_local = (
            {}
            if feedback_class is FeedbackClass.SOFT_JUDGE
            else {"correct_answer": "private task-local answer"}
        )
        return FeedbackPartition(
            feedback_class=feedback_class,
            global_feedback={"general_lesson": "validate report completeness"},
            task_local_feedback=task_local,
        )

    operation = DurableEvaluatorOperation(
        tmp_path / feedback_class.value,
        executor=CountingExecutor(),
        partitioner=partitioner,
    )
    result = operation.execute(
        request=_request(),
        idempotency_key=f"formal-v11-{feedback_class.value}",
        policy=_policy(),
    )
    public = json.dumps(result.public_receipt)
    assert "correct_answer" not in public
    private = operation.read_feedback_for_attachment(
        idempotency_key=f"formal-v11-{feedback_class.value}",
        expected_sha256=result.private_feedback_sha256,
    )
    assert private["feedback_class"] == feedback_class.value
    assert bool(private["task_local_feedback"]) is (feedback_class is not FeedbackClass.SOFT_JUDGE)


def test_global_feedback_rejects_private_and_task_specific_content(tmp_path: Path) -> None:
    def private_partitioner(_request, _outcome):
        return FeedbackPartition(
            feedback_class=FeedbackClass.MIXED,
            global_feedback={"judge_reasoning": "must stay private"},
            task_local_feedback={"answer": "local"},
        )

    operation = DurableEvaluatorOperation(
        tmp_path / "private", executor=CountingExecutor(), partitioner=private_partitioner
    )
    with pytest.raises(EvaluationIntegrityError, match="private evaluator"):
        operation.execute(
            request=_request(), idempotency_key="formal-v11-private", policy=_policy()
        )

    def task_partitioner(_request, _outcome):
        return FeedbackPartition(
            feedback_class=FeedbackClass.MIXED,
            global_feedback={"lesson": "Astronomy_004 should use its answer"},
            task_local_feedback={"answer": "local"},
        )

    task_operation = DurableEvaluatorOperation(
        tmp_path / "task", executor=CountingExecutor(), partitioner=task_partitioner
    )
    with pytest.raises(EvaluationIntegrityError, match="task-specific"):
        task_operation.execute(
            request=_request(),
            idempotency_key="formal-v11-task-specific",
            policy=_policy(),
            task_specific_literals=("Astronomy_004",),
        )


def test_secret_material_never_enters_authority_or_mirrors(tmp_path: Path) -> None:
    secret = "synthetic-secret-value-never-persist"
    outcome = _outcome()
    poisoned = replace(
        outcome,
        raw_score={"total_score": 50.0, "items": [{"reasoning": secret}]},
    )
    operation = DurableEvaluatorOperation(
        tmp_path / "evaluator", executor=CountingExecutor(poisoned)
    )
    with pytest.raises(EvaluationIntegrityError, match="secret material"):
        operation.execute(
            request=_request(),
            idempotency_key="formal-v11-secret",
            policy=_policy(),
            secret_values=(secret,),
        )
    for path in (tmp_path / "evaluator").rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes()
    assert redact_text(f"prefix {secret} suffix", (secret,)) == "prefix [REDACTED] suffix"


def test_request_secret_field_is_rejected_before_judge(tmp_path: Path) -> None:
    executor = CountingExecutor()
    operation = DurableEvaluatorOperation(tmp_path / "evaluator", executor=executor)
    with pytest.raises(EvaluationIntegrityError, match="secret field"):
        operation.execute(
            request=_request(JUDGE_API_KEY="must-not-enter-request"),
            idempotency_key="formal-v11-request-secret",
            policy=_policy(),
        )
    assert executor.calls == 0


def test_private_feedback_hash_and_mirror_tamper_fail_closed(tmp_path: Path) -> None:
    operation = DurableEvaluatorOperation(tmp_path / "evaluator", executor=CountingExecutor())
    result = operation.execute(
        request=_request(), idempotency_key="formal-v11-tamper", policy=_policy()
    )
    with pytest.raises(EvaluationIntegrityError, match="hash differs"):
        operation.read_feedback_for_attachment(
            idempotency_key="formal-v11-tamper", expected_sha256="0" * 64
        )
    raw_path = operation.store.private_root / result.operation_id / "raw_score.json"
    raw_path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(EvaluationIntegrityError, match="mirror differs"):
        operation.recover(
            request=_request(),
            idempotency_key="formal-v11-tamper",
            policy=_policy(),
        )


def test_evaluator_authority_permissions_are_private(tmp_path: Path) -> None:
    operation = DurableEvaluatorOperation(tmp_path / "evaluator", executor=CountingExecutor())
    operation.execute(
        request=_request(), idempotency_key="formal-v11-permissions", policy=_policy()
    )
    assert stat_mode(operation.store.root) == 0o700
    assert stat_mode(operation.store.db_path) == 0o600
    for path in operation.store.private_root.rglob("*.json"):
        assert stat_mode(path) == 0o600


def stat_mode(path: Path) -> int:
    return os.stat(path, follow_symlinks=False).st_mode & 0o777


def test_detailed_community_evaluator_preserves_slug_and_marks_metrics_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    project_root = tmp_path / "project"
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    project_root.mkdir()
    workspace.mkdir()
    private.mkdir()
    monkeypatch.setenv("JUDGE_API_KEY", "not-persisted")
    monkeypatch.setenv("JUDGE_API_BASE", OPENROUTER_API_BASE)
    monkeypatch.setenv("JUDGE_MODEL_NAME", "openai/gpt-5.1")
    observed = {}

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["env"] = kwargs["env"]
        raw = Path(command[command.index("--raw-output") + 1])
        metadata = Path(command[command.index("--execution-metadata") + 1])
        raw.write_text(json.dumps({"total_score": 61.0, "items": []}), encoding="utf-8")
        metadata.write_text(
            json.dumps(
                {
                    "api_base": OPENROUTER_API_BASE,
                    "api_key_present": True,
                    "cost_total_usd": UNAVAILABLE,
                    "model": "openai/gpt-5.1",
                    "provider": "openai_compatible",
                    "request_count": UNAVAILABLE,
                    "secret_recorded": False,
                    "usage": UNAVAILABLE,
                }
            ),
            encoding="utf-8",
        )
        return Completed()

    monkeypatch.setattr(evaluator_module.subprocess, "run", fake_run)
    result = run_community_evaluator_detailed(
        project_root=project_root,
        workspace=workspace,
        evaluator_private_root=private,
        task_id="Astronomy_004",
        attempt_id="Astronomy_004_a0_v11",
        expected_model="openai/gpt-5.1",
        expected_api_base=OPENROUTER_API_BASE,
        expected_provider="openai_compatible",
    )
    assert (
        observed["command"][observed["command"].index("--expected-judge-model") + 1]
        == "openai/gpt-5.1"
    )
    assert (
        observed["command"][observed["command"].index("--expected-judge-api-base") + 1]
        == OPENROUTER_API_BASE
    )
    assert result.judge_outcome.request_count == UNAVAILABLE
    assert result.judge_outcome.usage == UNAVAILABLE
    assert result.judge_outcome.cost_total_usd == UNAVAILABLE
    # The key is passed only to the evaluator child, never written into a
    # command, receipt, or metadata object.
    assert "not-persisted" not in json.dumps(observed["command"])
    assert observed["env"]["JUDGE_API_KEY"] == "not-persisted"


def test_detailed_community_evaluator_rejects_worker_identity_drift(
    tmp_path: Path, monkeypatch
) -> None:
    project_root = tmp_path / "project"
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    project_root.mkdir()
    workspace.mkdir()
    private.mkdir()
    monkeypatch.setenv("JUDGE_API_KEY", "not-persisted")
    monkeypatch.setenv("JUDGE_API_BASE", OPENROUTER_API_BASE)
    monkeypatch.setenv("JUDGE_MODEL_NAME", "openai/gpt-5.1")

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command, **_kwargs):
        raw = Path(command[command.index("--raw-output") + 1])
        metadata = Path(command[command.index("--execution-metadata") + 1])
        raw.write_text(json.dumps({"total_score": 61.0}), encoding="utf-8")
        metadata.write_text(
            json.dumps(
                {
                    "api_base": OPENROUTER_API_BASE,
                    "api_key_present": True,
                    "cost_total_usd": UNAVAILABLE,
                    "model": "gpt-5.1",
                    "provider": "openai_compatible",
                    "request_count": UNAVAILABLE,
                    "secret_recorded": False,
                    "usage": UNAVAILABLE,
                }
            ),
            encoding="utf-8",
        )
        return Completed()

    monkeypatch.setattr(evaluator_module.subprocess, "run", fake_run)
    with pytest.raises(EvaluatorError, match="identity differs"):
        run_community_evaluator_detailed(
            project_root=project_root,
            workspace=workspace,
            evaluator_private_root=private,
            task_id="Astronomy_004",
            attempt_id="Astronomy_004_a0_v11",
            expected_model="openai/gpt-5.1",
            expected_api_base=OPENROUTER_API_BASE,
            expected_provider="openai_compatible",
        )


def test_durable_community_executor_binds_invocation_and_protocol(
    tmp_path: Path, monkeypatch
) -> None:
    policy = _policy()
    observed = {}

    def fake_detailed(**kwargs):
        observed.update(kwargs)
        return CommunityEvaluatorExecution(
            raw_score=_outcome().raw_score, judge_outcome=_outcome()
        )

    monkeypatch.setattr(evaluator_module, "run_community_evaluator_detailed", fake_detailed)
    executor = DurableCommunityJudgeExecutor(
        project_root=tmp_path,
        evaluator_private_root=tmp_path / "private",
        policy=policy,
    )
    invocation = evaluator_module.JudgeInvocation(
        operation_id="evaluation-1",
        idempotency_key="formal-v11",
        request_sha256="1" * 64,
        policy_sha256=policy.policy_sha256,
        model=policy.model,
        provider=policy.provider,
        api_base=policy.api_base,
        scorer_commit=policy.scorer_commit,
        scorer_sha256=policy.scorer_sha256,
    )
    outcome = executor(
        _request(candidate_output_root=os.fspath(tmp_path / "candidate")), invocation
    )
    assert outcome.raw_score["total_score"] == 73.5
    assert observed["expected_model"] == "openai/gpt-5.1"
    assert observed["expected_api_base"] == OPENROUTER_API_BASE
    assert observed["expected_provider"] == "openai_compatible"
    with pytest.raises(EvaluatorError, match="identity differs"):
        executor(
            _request(candidate_output_root=os.fspath(tmp_path)),
            replace(invocation, model="gpt-5.1"),
        )


def test_scorer_tracked_tree_hash_binds_commit_and_tree_object(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "ResearchClawBench"
    root.mkdir()
    responses = iter(("5" * 40, "", "a" * 40))

    class Result:
        returncode = 0
        stderr = ""

        def __init__(self):
            self.stdout = next(responses) + "\n"

    monkeypatch.setattr(evaluator_module.subprocess, "run", lambda *_a, **_k: Result())
    first = scorer_tracked_tree_sha256(root, expected_commit="5" * 40)
    assert len(first) == 64

    responses = iter(("5" * 40, " M evaluation/score.py", "a" * 40))
    with pytest.raises(EvaluatorError, match="tracked tree is dirty"):
        scorer_tracked_tree_sha256(root, expected_commit="5" * 40)


def test_production_builder_constructs_policy_executor_and_durable_store(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"
    experiment = project / "experiment"
    benchmark = project / "ResearchClawBench"
    experiment.mkdir(parents=True)
    benchmark.mkdir()

    class Config:
        project_root = project
        experiment_root = experiment
        researchclawbench_root = benchmark

        def require(self, key):
            if key == "judge.scorer_commit":
                return "5" * 40
            if key == "judge":
                return {
                    "model": "openai/gpt-5.1",
                    "provider": "openai_compatible",
                    "model_source": "OpenRouter_model_catalog",
                    "api_key_env": "JUDGE_API_KEY",
                    "api_base_env": "JUDGE_API_BASE",
                    "model_env": "JUDGE_MODEL_NAME",
                    "runs_per_attempt": 1,
                    "scorer_commit": "5" * 40,
                }
            raise KeyError(key)

    monkeypatch.setattr(
        evaluator_module,
        "scorer_tracked_tree_sha256",
        lambda *_args, **_kwargs: "6" * 64,
    )
    bundle = build_production_community_evaluator(
        config=Config(),
        authority_root=experiment / "evaluator_private",  # type: ignore[arg-type]
    )
    assert bundle.policy.model == "openai/gpt-5.1"
    assert bundle.scorer_tracked_tree_sha256 == "6" * 64
    assert isinstance(bundle.executor, DurableCommunityJudgeExecutor)
    assert bundle.operation.store.db_path.is_file()
    assert stat_mode(bundle.operation.store.db_path) == 0o600


def test_production_evaluator_port_is_directly_compatible_and_durable(
    tmp_path: Path,
) -> None:
    policy = _policy()
    executor = CountingExecutor()
    operation = DurableEvaluatorOperation(tmp_path / "authority", executor=executor)
    bundle = ProductionCommunityEvaluatorBundle(
        policy=policy,
        executor=None,  # type: ignore[arg-type]
        operation=operation,
        scorer_tracked_tree_sha256=policy.scorer_sha256,
    )
    port = DurableCommunityEvaluatorPort(bundle)
    request = {
        "candidate": {
            "run_id": "Astronomy_004_a0_v11",
            "session_id": "session-fresh-v11",
            "dataset_id": "dataset-fresh-v11",
            "dataset_revision": "art_fresh.v1",
            "runtime_seconds": 121,
            "candidate_output_root": os.fspath(tmp_path / "candidate"),
        },
        "validation": {
            "content_sha256": "7" * 64,
            "artifact_root_sha256": "8" * 64,
        },
        "evaluator_only": True,
    }
    first = port.execute(request, "formal-v11-port")
    second = port.recover(request, "formal-v11-port")
    assert executor.calls == 1
    assert second == first
    assert first["completed"] is True
    assert first["artifact_valid"] is True
    assert first["cost_total_usd"] == UNAVAILABLE
    private = port.read_feedback_for_attachment(
        idempotency_key="formal-v11-port",
        expected_sha256=first["private_feedback_sha256"],
    )
    assert private["feedback_class"] == "SOFT_JUDGE"
