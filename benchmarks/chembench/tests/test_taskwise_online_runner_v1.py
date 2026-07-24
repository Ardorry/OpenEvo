from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

import pytest

from openevo_chembench.chembench4k_models import (
    CHEMBENCH4K_REVISION,
    PrivateChemBench4KTask,
    RenderedChemBench4KPrompt,
)
from openevo_chembench.frozen_runtime_v2 import (
    CoreResolvedTextMemoryV2,
    _issue_core_resolved_text_memory_v2,
)
from openevo_chembench.models import RawAttempt, TranscriptReference
from openevo_chembench.taskwise_config_v1 import TASKWISE_MEMORY_LIMITS_V1
from openevo_chembench.taskwise_context_binding_v1 import (
    CONTEXT_BINDING_VIOLATION,
    TaskwiseContextBindingReceiptV1,
    TaskwiseSessionContextBindingV1,
    issue_taskwise_context_binding_receipt_v1,
)
from openevo_chembench.taskwise_online_runner_v1 import (
    CoreMemoryReferenceV1,
    TaskwiseAgentRequestV1,
    TaskwiseCoreUpdateOutcomeV1,
    TaskwiseEpisodeStateV1,
    TaskwiseEpisodeV1,
    TaskwiseMemoryPublicMetricsV1,
    TaskwiseOnlineRunnerV1,
    TaskwiseRunConfigV1,
    TaskwiseRunStatusV1,
    TaskwiseRunnerCoreUpdateRequestV1,
    TASKWISE_EXECUTOR_FAILURE_CODES_V1,
    _chain_rows,
    load_private_taskwise_results_v1,
)


_DATASET_SHA256 = "d" * 64
_POLICY_SHA256 = "e" * 64


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _episodes(count: int = 2) -> tuple[TaskwiseEpisodeV1, ...]:
    result: list[TaskwiseEpisodeV1] = []
    for index in range(count):
        uid = _sha256(f"task:{index}")
        task = PrivateChemBench4KTask(
            uid=uid,
            category="Name_Conversion",
            source_split="test",
            source_index=index,
            question=f"Public question {index}?",
            A="choice-a",
            B="choice-b",
            C="choice-c",
            D="choice-d",
            target="A",
            dataset_revision=CHEMBENCH4K_REVISION,
            dataset_sha256=_DATASET_SHA256,
        )
        prompt = RenderedChemBench4KPrompt(
            uid=uid,
            category=task.category,
            dataset_revision=CHEMBENCH4K_REVISION,
            demonstration_uids=(),
            text=f"Rendered public prompt {index}\nAnswer:",
        )
        result.append(TaskwiseEpisodeV1(task=task, prompt=prompt))
    return tuple(result)


def _config(tmp_path: Path, arm: str) -> TaskwiseRunConfigV1:
    return TaskwiseRunConfigV1(
        arm=arm,
        run_id=f"taskwise_{arm}_run",
        output_directory=(tmp_path / arm).resolve(),
        protocol_sha256="1" * 64,
        source_commit="2" * 40,
        dataset_sha256=_DATASET_SHA256,
        task_manifest_sha256="3" * 64,
        model_identity_sha256="4" * 64,
        executor_policy_sha256=_POLICY_SHA256,
    )


def _memory(name: str) -> CoreResolvedTextMemoryV2:
    markdown = f"## Do\n{name}\n"
    return _issue_core_resolved_text_memory_v2(
        core_artifact_id=f"artifact_{name}",
        artifact_payload_sha256=_sha256(f"payload:{name}"),
        context_resolution_digest=_sha256(f"context:{name}"),
        resolved_memory_sha256=_sha256(markdown),
        markdown=markdown,
    )


def _memory_metrics(name: str) -> TaskwiseMemoryPublicMetricsV1:
    return TaskwiseMemoryPublicMetricsV1(
        memory_limits_sha256=TASKWISE_MEMORY_LIMITS_V1.digest,
        inspection_sha256=_sha256(f"inspection:{name}"),
        token_estimator_id=TASKWISE_MEMORY_LIMITS_V1.token_estimator_id,
        parser_id=TASKWISE_MEMORY_LIMITS_V1.parser_id,
        utf8_byte_count=256,
        estimated_token_count=40,
        section_item_counts=(1, 1, 1, 1, 1),
        total_section_items=5,
        max_section_items=1,
    )


class _DummyExecutor:
    def __init__(self, outcomes: list[str | Exception]) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[TaskwiseAgentRequestV1] = []
        self.context_receipts: dict[str, TaskwiseContextBindingReceiptV1] = {}

    def execute_taskwise(self, request: TaskwiseAgentRequestV1) -> RawAttempt:
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        binding = TaskwiseSessionContextBindingV1.from_memory(
            session_id=request.session_id,
            memory=request.resolved_text_memory,
        )
        self.context_receipts[request.session_id] = issue_taskwise_context_binding_receipt_v1(
            expected=binding,
            actual=binding,
        )
        return RawAttempt(
            response=outcome,
            transcript_reference=TranscriptReference(
                reference=f"private-transcript-{request.session_id}"
            ),
        )

    def consume_taskwise_context_receipt(
        self,
        session_id: str,
    ) -> TaskwiseContextBindingReceiptV1:
        return self.context_receipts.pop(session_id)


class _MismatchedContextExecutor(_DummyExecutor):
    def execute_taskwise(self, request: TaskwiseAgentRequestV1) -> RawAttempt:
        attempt = super().execute_taskwise(request)
        expected = TaskwiseSessionContextBindingV1.from_memory(
            session_id=request.session_id,
            memory=request.resolved_text_memory,
        )
        actual = TaskwiseSessionContextBindingV1.from_memory(
            session_id="session_forged_9999",
            memory=request.resolved_text_memory,
        )
        self.context_receipts[request.session_id] = issue_taskwise_context_binding_receipt_v1(
            expected=expected,
            actual=actual,
        )
        return attempt


class _MissingContextReceiptExecutor(_DummyExecutor):
    def execute_taskwise(self, request: TaskwiseAgentRequestV1) -> RawAttempt:
        attempt = super().execute_taskwise(request)
        self.context_receipts.pop(request.session_id)
        return attempt


class _DummyCorePort:
    def __init__(self) -> None:
        self.requests: list[TaskwiseRunnerCoreUpdateRequestV1] = []
        self.memories: dict[CoreMemoryReferenceV1, CoreResolvedTextMemoryV2] = {}

    def update_text_memory(
        self,
        request: TaskwiseRunnerCoreUpdateRequestV1,
    ) -> TaskwiseCoreUpdateOutcomeV1:
        self.requests.append(request)
        memory = _memory(f"{request.task_index}_{request.update_index}")
        self.memories[CoreMemoryReferenceV1.from_memory(memory)] = memory
        return TaskwiseCoreUpdateOutcomeV1(
            job_id=f"core_job_{request.task_index}_{request.update_index}",
            job_state="COMPLETED",
            core_context_id=f"context_{request.task_index}_{request.update_index}",
            validation_receipt_sha256=_sha256(
                f"validation:{request.task_index}:{request.update_index}"
            ),
            memory_metrics=_memory_metrics(f"{request.task_index}_{request.update_index}"),
            resolved_text_memory=memory,
        )

    def resolve_text_memory(
        self,
        reference: CoreMemoryReferenceV1,
    ) -> CoreResolvedTextMemoryV2:
        return self.memories[reference]


class _CoreSecurityViolation(RuntimeError):
    finding_code = "TASKWISE_REFLECTOR_SECURITY_TOOL_USE_VIOLATION"


class _ExecutorFailure(RuntimeError):
    def __init__(
        self,
        *,
        code: str,
        completion_observed: bool,
        diagnostic_receipt: str | None = None,
        event_digest: str | None = None,
        event_counts: dict[str, int] | None = None,
        executor_stage: str | None = None,
        message: str = "PRIVATE_EXECUTOR_BODY_SENTINEL",
    ) -> None:
        self.taskwise_failure_code = code
        self.completion_observed = completion_observed
        self.diagnostic_receipt = diagnostic_receipt
        self.event_digest = event_digest
        self.event_counts = {} if event_counts is None else event_counts
        self.executor_stage = executor_stage
        self.run_status = (
            "SECURITY_TOOL_USE_VIOLATION"
            if code == "EXECUTOR_SECURITY_TOOL_USE_VIOLATION"
            else None
        )
        super().__init__(message)


class _FailingCorePort:
    def update_text_memory(
        self,
        _request: TaskwiseRunnerCoreUpdateRequestV1,
    ) -> TaskwiseCoreUpdateOutcomeV1:
        raise _CoreSecurityViolation

    def resolve_text_memory(
        self,
        _reference: CoreMemoryReferenceV1,
    ) -> CoreResolvedTextMemoryV2:
        raise AssertionError("security-terminated Core state must not resolve")


class _PrivateFailurePersistenceFailingRunner(TaskwiseOnlineRunnerV1):
    def _append_private_failure(self, _exc: object) -> str:
        raise OSError("PRIVATE_DIAGNOSTIC_PERSISTENCE_BODY_SENTINEL")


class _UnresolvableCorePort(_DummyCorePort):
    def resolve_text_memory(
        self,
        _reference: CoreMemoryReferenceV1,
    ) -> CoreResolvedTextMemoryV2:
        raise RuntimeError("synthetic Core artifact disappeared")


class _UpdateTwoFailingCorePort(_DummyCorePort):
    def update_text_memory(
        self,
        request: TaskwiseRunnerCoreUpdateRequestV1,
    ) -> TaskwiseCoreUpdateOutcomeV1:
        if request.update_index == 2:
            self.requests.append(request)
            raise RuntimeError("synthetic update-two validation failure")
        return super().update_text_memory(request)


class _SentinelCorePort(_DummyCorePort):
    memory_sentinel = "PRIVATE_MEMORY_BODY_SENTINEL_71F4C2"

    def update_text_memory(
        self,
        request: TaskwiseRunnerCoreUpdateRequestV1,
    ) -> TaskwiseCoreUpdateOutcomeV1:
        self.requests.append(request)
        markdown = f"""# General Chemistry Memory

## Do
- {self.memory_sentinel}

## Avoid
- Avoid premature selection.

## Validate
- Validate units and structure.

## When Applicable
- Apply dimensional checks.

## Retired Or Superseded
- Retire superseded general advice.
"""
        name = f"{request.task_index}_{request.update_index}"
        memory = _issue_core_resolved_text_memory_v2(
            core_artifact_id=f"artifact_{name}",
            artifact_payload_sha256=_sha256(f"payload:{name}"),
            context_resolution_digest=_sha256(f"context:{name}"),
            resolved_memory_sha256=_sha256(markdown),
            markdown=markdown,
        )
        self.memories[CoreMemoryReferenceV1.from_memory(memory)] = memory
        return TaskwiseCoreUpdateOutcomeV1(
            job_id=f"core_job_{name}",
            job_state="COMPLETED",
            core_context_id=f"context_{name}",
            validation_receipt_sha256=_sha256(f"validation:{name}"),
            memory_metrics=TaskwiseMemoryPublicMetricsV1(
                memory_limits_sha256=TASKWISE_MEMORY_LIMITS_V1.digest,
                inspection_sha256=_sha256(f"inspection:{name}"),
                token_estimator_id=TASKWISE_MEMORY_LIMITS_V1.token_estimator_id,
                parser_id=TASKWISE_MEMORY_LIMITS_V1.parser_id,
                utf8_byte_count=len(markdown.encode("utf-8")),
                estimated_token_count=48,
                section_item_counts=(1, 1, 1, 1, 1),
                total_section_items=5,
                max_section_items=1,
            ),
            resolved_text_memory=memory,
        )


class _SimulatedProcessLoss(BaseException):
    pass


class _PauseAfterStateRunner(TaskwiseOnlineRunnerV1):
    def __init__(self, *args, pause_state: TaskwiseEpisodeStateV1, **kwargs) -> None:
        self._pause_state = pause_state
        self._paused = False
        super().__init__(*args, **kwargs)

    def _drive(self) -> None:
        if not self._paused and self._pause_state is TaskwiseEpisodeStateV1.TASK_OPENED:
            self._paused = True
            raise _SimulatedProcessLoss
        super()._drive()

    def _transition(self, target: TaskwiseEpisodeStateV1) -> None:
        super()._transition(target)
        if not self._paused and target is self._pause_state:
            self._paused = True
            raise _SimulatedProcessLoss


def _session_factory(prefix: str):
    counter = 0

    def issue() -> str:
        nonlocal counter
        counter += 1
        return f"session_{prefix}_{counter:04d}"

    return issue


def _recursive_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(
            *((_recursive_keys(item) for item in value.values())),
        )
    if isinstance(value, list):
        return set().union(*(_recursive_keys(item) for item in value))
    return set()


def test_episode_state_machine_has_the_exact_seven_states() -> None:
    assert [state.value for state in TaskwiseEpisodeStateV1] == [
        "TASK_OPENED",
        "ROUND_0_COMPLETED",
        "UPDATE_1_COMPLETED",
        "ROUND_1_COMPLETED",
        "UPDATE_2_COMPLETED",
        "ROUND_2_COMPLETED",
        "TASK_FINALIZED",
    ]


def test_agent_request_projects_session_and_round_out_of_prompt_payload() -> None:
    memory = _memory("projection")
    task_uid = _episodes(1)[0].task.uid
    request = TaskwiseAgentRequestV1(
        rendered_public_prompt="Only this public prompt reaches Codex.",
        resolved_text_memory=memory,
        session_id="session_projection_0001",
        arm="online",
        task_ordinal=3,
        round_index=1,
        run_id="taskwise_online_run",
        task_uid=task_uid,
    )

    projected = request.to_frozen_agent_request()

    assert projected.rendered_public_prompt == request.rendered_public_prompt
    assert projected.resolved_text_memory is memory
    payload = projected.to_runtime_payload()
    serialized = json.dumps(payload, sort_keys=True)
    assert "session_projection" not in serialized
    assert "task_ordinal" not in serialized
    assert "round_index" not in serialized
    assert request.run_id not in serialized
    assert task_uid not in serialized


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("run_id", "../unsafe"),
        ("task_uid", "not-a-sha256"),
    ),
)
def test_agent_request_rejects_invalid_private_diagnostic_identifiers(
    field_name: str,
    value: str,
) -> None:
    kwargs = {
        "rendered_public_prompt": "Public prompt.",
        "resolved_text_memory": None,
        "session_id": "session_projection_0002",
        "arm": "control",
        "task_ordinal": 0,
        "round_index": 0,
        "run_id": "taskwise_control_run",
        "task_uid": _episodes(1)[0].task.uid,
    }
    kwargs[field_name] = value

    with pytest.raises(ValueError):
        TaskwiseAgentRequestV1(**kwargs)


def test_control_runs_three_fresh_sessions_per_task_without_memory_or_updates(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, "control")
    episodes = _episodes()
    executor = _DummyExecutor(["A"] * 6)
    runner = TaskwiseOnlineRunnerV1(
        config=config,
        episodes=episodes,
        executor=executor,
        session_id_factory=_session_factory("control"),
    )

    result = runner.run()

    assert result.status is TaskwiseRunStatusV1.COMPLETED
    assert result.completed_tasks == 2
    assert result.completion_count == 6
    assert result.update_count == 0
    assert result.core_job_count == 0
    assert result.core_artifact_count == 0
    assert result.context_binding_violation_count == 0
    assert result.memory_aggregate.approved_artifact_count == 0
    assert result.session_attempt_count == 6
    assert len({request.session_id for request in executor.requests}) == 6
    assert [request.round_index for request in executor.requests] == [0, 1, 2, 0, 1, 2]
    assert all(request.run_id == config.run_id for request in executor.requests)
    assert [request.task_uid for request in executor.requests] == [
        episodes[0].task.uid,
        episodes[0].task.uid,
        episodes[0].task.uid,
        episodes[1].task.uid,
        episodes[1].task.uid,
        episodes[1].task.uid,
    ]
    assert all(request.resolved_text_memory is None for request in executor.requests)
    assert [request.rendered_public_prompt for request in executor.requests[:3]] == [
        "Rendered public prompt 0\nAnswer:"
    ] * 3
    assert [request.rendered_public_prompt for request in executor.requests[3:]] == [
        "Rendered public prompt 1\nAnswer:"
    ] * 3
    public_text = (result.output_directory / "public" / "events.jsonl").read_text()
    assert '"kind":"core_update"' not in public_text
    assert '"target"' not in public_text
    assert '"raw_completion"' not in public_text
    public_rows = [
        json.loads(line)
        for line in (result.output_directory / "public" / "events.jsonl").read_text().splitlines()
    ]
    assert all(row["memory"] is None for row in public_rows)
    assert all(
        row["context_binding"]["expected_memory_artifact_id"] is None
        and row["context_binding"]["actual_resolved_artifact_id"] is None
        and row["context_binding"]["expected_memory_sha256"] is None
        and row["context_binding"]["actual_injected_memory_sha256"] is None
        and row["context_binding"]["context_resolution_digest"] is None
        and row["context_binding"]["session_id"] == row["session_id"]
        and row["context_binding"]["passed"] is True
        for row in public_rows
    )
    private_rows = [
        json.loads(line)
        for line in (result.output_directory / "private" / "evaluations.jsonl")
        .read_text()
        .splitlines()
    ]
    assert all(row["trajectory"] is None for row in private_rows)
    failures_path = result.output_directory / "private" / "failures.jsonl"
    assert failures_path.read_bytes() == b""
    assert stat.S_IMODE(failures_path.stat().st_mode) == 0o600


def test_online_uses_complete_prefixes_and_carries_only_update_two(
    tmp_path: Path,
) -> None:
    executor = _DummyExecutor(["B", "A", "A", "B", "A", "A"])
    core = _DummyCorePort()
    runner = TaskwiseOnlineRunnerV1(
        config=_config(tmp_path, "online"),
        episodes=_episodes(),
        executor=executor,
        core_update_port=core,
        session_id_factory=_session_factory("online"),
    )

    result = runner.run()

    assert result.status is TaskwiseRunStatusV1.COMPLETED
    assert result.completion_count == 6
    assert result.update_count == 4
    assert result.core_job_count == 4
    assert result.core_artifact_count == 4
    assert result.context_binding_violation_count == 0
    assert result.memory_aggregate.approved_artifact_count == 4
    assert result.memory_aggregate.max_utf8_byte_count == 256
    assert result.memory_aggregate.max_estimated_token_count == 40
    assert len({request.session_id for request in executor.requests}) == 6
    assert [request.rendered_public_prompt for request in executor.requests[:3]] == [
        _episodes()[0].prompt.text
    ] * 3
    assert [request.rendered_public_prompt for request in executor.requests[3:]] == [
        _episodes()[1].prompt.text
    ] * 3
    assert [
        (
            None
            if request.resolved_text_memory is None
            else request.resolved_text_memory.core_artifact_id
        )
        for request in executor.requests
    ] == [
        None,
        "artifact_0_1",
        "artifact_0_2",
        "artifact_0_2",
        "artifact_1_1",
        "artifact_1_2",
    ]
    assert [len(request.trajectories) for request in core.requests] == [1, 2, 1, 2]
    assert [
        tuple(item.round_index for item in request.trajectories) for request in core.requests
    ] == [(0,), (0, 1), (0,), (0, 1)]
    assert all(
        request.safe_signals == tuple(item.safe_feedback for item in request.trajectories)
        for request in core.requests
    )
    assert [
        (
            None
            if request.prior_resolved_text_memory is None
            else request.prior_resolved_text_memory.core_artifact_id
        )
        for request in core.requests
    ] == [
        None,
        "artifact_0_1",
        "artifact_0_2",
        "artifact_1_1",
    ]
    assert all(
        item.task_uid == request.task_uid and item.task_index == request.task_index
        for request in core.requests
        for item in request.trajectories
    )
    private_results = load_private_taskwise_results_v1(result.output_directory)
    assert len(private_results) == 6
    assert [row.core_artifact_id for row in private_results] == [
        None,
        "artifact_0_1",
        "artifact_0_2",
        "artifact_0_2",
        "artifact_1_1",
        "artifact_1_2",
    ]
    public_rows = [
        json.loads(line)
        for line in (result.output_directory / "public" / "events.jsonl").read_text().splitlines()
    ]
    completion_rows = [row for row in public_rows if row["kind"] == "completion"]
    expected_memories = [
        None,
        _memory("0_1"),
        _memory("0_2"),
        _memory("0_2"),
        _memory("1_1"),
        _memory("1_2"),
    ]
    for row, expected_memory in zip(
        completion_rows,
        expected_memories,
        strict=True,
    ):
        binding = row["context_binding"]
        assert binding["session_id"] == row["session_id"]
        assert binding["expected_session_id"] == row["session_id"]
        assert binding["passed"] is True
        assert binding["finding_codes"] == []
        if expected_memory is None:
            assert binding["expected_memory_artifact_id"] is None
            assert binding["actual_resolved_artifact_id"] is None
            assert binding["expected_memory_sha256"] is None
            assert binding["actual_injected_memory_sha256"] is None
            assert binding["context_resolution_digest"] is None
        else:
            assert (
                binding["expected_memory_artifact_id"]
                == binding["actual_resolved_artifact_id"]
                == expected_memory.core_artifact_id
            )
            assert (
                binding["expected_memory_sha256"]
                == binding["actual_injected_memory_sha256"]
                == expected_memory.resolved_memory_sha256
            )
            assert (
                binding["context_resolution_digest"] == expected_memory.context_resolution_digest
            )


@pytest.mark.parametrize(
    "executor_type",
    (_MismatchedContextExecutor, _MissingContextReceiptExecutor),
)
def test_context_binding_mismatch_fails_closed_before_private_evaluation(
    tmp_path: Path,
    executor_type,
) -> None:
    executor = executor_type(["A", "A", "A"])
    result = TaskwiseOnlineRunnerV1(
        config=_config(tmp_path, "control"),
        episodes=_episodes(1),
        executor=executor,
        session_id_factory=_session_factory("binding-violation"),
    ).run()

    assert result.status is TaskwiseRunStatusV1.TASKWISE_CONTEXT_BINDING_VIOLATION
    assert result.finding_codes == (CONTEXT_BINDING_VIOLATION,)
    assert result.resume_allowed is False
    assert result.completion_count == 0
    assert result.session_attempt_count == 1
    assert result.context_binding_violation_count == 1
    assert len(executor.requests) == 1
    assert not (result.output_directory / "private" / "evaluations.jsonl").read_bytes()
    public_rows = [
        json.loads(line)
        for line in (result.output_directory / "public" / "events.jsonl").read_text().splitlines()
    ]
    assert len(public_rows) == 1
    assert public_rows[0]["kind"] == "context_binding_violation"
    assert public_rows[0]["finding_codes"] == [CONTEXT_BINDING_VIOLATION]
    assert "target" not in json.dumps(public_rows[0], sort_keys=True)


def test_public_run_surfaces_never_persist_memory_markdown(
    tmp_path: Path,
) -> None:
    core = _SentinelCorePort()
    result = TaskwiseOnlineRunnerV1(
        config=_config(tmp_path, "online"),
        episodes=_episodes(1),
        executor=_DummyExecutor(["B", "A", "A"]),
        core_update_port=core,
        session_id_factory=_session_factory("memory-redaction"),
    ).run()

    public_rows = [
        json.loads(line)
        for line in (result.output_directory / "public" / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    state = json.loads((result.output_directory / "run_state.json").read_text(encoding="utf-8"))
    public_result = result.to_public_dict()
    for payload in (public_rows, state, public_result):
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        assert _SentinelCorePort.memory_sentinel not in encoded
        assert "General Chemistry Memory" not in encoded
        assert "## Do" not in encoded
        keys = _recursive_keys(payload)
        assert "markdown" not in keys
        assert "resolved_memory" not in keys
    assert result.memory_aggregate.approved_artifact_count == 2
    assert result.memory_aggregate.max_utf8_byte_count > 0


def test_correct_round_zero_does_not_trigger_best_of_or_early_stop(
    tmp_path: Path,
) -> None:
    executor = _DummyExecutor(["A", "B", "C"])
    core = _DummyCorePort()
    result = TaskwiseOnlineRunnerV1(
        config=_config(tmp_path, "online"),
        episodes=_episodes(1),
        executor=executor,
        core_update_port=core,
        session_id_factory=_session_factory("noearly"),
    ).run()

    assert result.status is TaskwiseRunStatusV1.COMPLETED
    assert result.completion_count == 3
    assert result.update_count == 2
    assert [request.round_index for request in executor.requests] == [0, 1, 2]
    results = load_private_taskwise_results_v1(result.output_directory)
    assert [row.official_prediction for row in results] == ["A", "B", "C"]
    assert results[-1].correct is False


def test_no_uppercase_completion_remains_an_official_parse_failure(
    tmp_path: Path,
) -> None:
    result = TaskwiseOnlineRunnerV1(
        config=_config(tmp_path, "control"),
        episodes=_episodes(1),
        executor=_DummyExecutor(["option c", "option c", "option c"]),
        session_id_factory=_session_factory("no-uppercase"),
    ).run()

    rows = load_private_taskwise_results_v1(result.output_directory)

    assert [row.official_prediction for row in rows] == ["", "", ""]
    assert all(not row.official_parsed for row in rows)
    assert all(not row.correct for row in rows)


def test_precompletion_executor_failure_is_terminal_and_preserves_safe_metadata(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, "control")
    event_digest = _sha256("safe synthetic event stream")
    first_executor = _DummyExecutor(
        [
            "A",
            _ExecutorFailure(
                code="EXECUTOR_MODEL_TRANSPORT_FAILED",
                completion_observed=False,
                diagnostic_receipt="receipt_safe_0001.json",
                event_digest=event_digest,
                executor_stage="MODEL_TRANSPORT",
            ),
        ]
    )
    failed = TaskwiseOnlineRunnerV1(
        config=config,
        episodes=_episodes(1),
        executor=first_executor,
        session_id_factory=_session_factory("first"),
    ).run()

    assert failed.status is TaskwiseRunStatusV1.EXECUTION_FAILED
    assert failed.completion_count == 1
    assert failed.session_attempt_count == 2
    assert failed.resume_allowed is False
    assert failed.finding_codes == ("EXECUTOR_MODEL_TRANSPORT_FAILED",)
    state = json.loads((config.output_directory / "run_state.json").read_text())
    assert state["failure"]["code"] == "EXECUTOR_MODEL_TRANSPORT_FAILED"
    assert state["failure"]["completion_observed"] is False
    assert state["failure"]["diagnostic_receipt"] == "receipt_safe_0001.json"
    assert state["failure"]["event_digest"] == event_digest
    assert len(state["failure"]["diagnostic_record_sha256"]) == 64
    assert set(state["failure"]["diagnostic_record_sha256"]) <= set("0123456789abcdef")
    serialized_state = json.dumps(state, sort_keys=True)
    assert "PRIVATE_EXECUTOR_BODY_SENTINEL" not in serialized_state
    assert "event_counts" not in state["failure"]
    assert "executor_stage" not in state["failure"]

    failures_path = config.output_directory / "private" / "failures.jsonl"
    assert stat.S_IMODE(failures_path.stat().st_mode) == 0o600
    failures = [json.loads(line) for line in failures_path.read_text().splitlines()]
    assert failures == [
        {
            "schema_version": "taskwise_private_failure_v1",
            "protocol_id": "repeated_session_control_v1",
            "arm": "control",
            "run_id": config.run_id,
            "task_uid": _episodes(1)[0].task.uid,
            "task_ordinal": 0,
            "episode_state": TaskwiseEpisodeStateV1.ROUND_0_COMPLETED.value,
            "round_index": 1,
            "update_index": None,
            "session_id": "session_first_0002",
            "code": "EXECUTOR_MODEL_TRANSPORT_FAILED",
            "completion_observed": False,
            "security_violation": False,
            "executor_stage": "MODEL_TRANSPORT",
            "diagnostic_receipt": "receipt_safe_0001.json",
            "event_digest": event_digest,
            "event_counts": {},
            "retry_allowed": False,
            "resume_allowed": False,
            "replacement_completion_allowed": False,
        }
    ]
    assert "PRIVATE_EXECUTOR_BODY_SENTINEL" not in failures_path.read_text()
    assert "Rendered public prompt" not in failures_path.read_text()
    assert "choice-a" not in failures_path.read_text()

    with pytest.raises(RuntimeError, match="forbids resume"):
        TaskwiseOnlineRunnerV1(
            config=config,
            episodes=_episodes(1),
            executor=_DummyExecutor(["A", "A"]),
            resume=True,
            session_id_factory=_session_factory("resumed"),
        ).run()


@pytest.mark.parametrize("pause_state", tuple(TaskwiseEpisodeStateV1))
def test_online_resume_revalidates_every_persisted_state(
    tmp_path: Path,
    pause_state: TaskwiseEpisodeStateV1,
) -> None:
    config = _config(tmp_path / pause_state.value, "online")
    core = _DummyCorePort()
    first_executor = _DummyExecutor(["A"] * 3)
    with pytest.raises(_SimulatedProcessLoss):
        _PauseAfterStateRunner(
            config=config,
            episodes=_episodes(1),
            executor=first_executor,
            core_update_port=core,
            pause_state=pause_state,
            session_id_factory=_session_factory(f"pause-{pause_state.value}"),
        ).run()

    state = json.loads((config.output_directory / "run_state.json").read_text())
    assert state["status"] == TaskwiseRunStatusV1.RUNNING.value
    assert state["pending_invocation"] is None
    completed_rounds = state["completion_count"]
    resumed = TaskwiseOnlineRunnerV1(
        config=config,
        episodes=_episodes(1),
        executor=_DummyExecutor(["A"] * (3 - completed_rounds)),
        core_update_port=core,
        resume=True,
        session_id_factory=_session_factory(f"resume-{pause_state.value}"),
    ).run()

    assert resumed.status is TaskwiseRunStatusV1.COMPLETED
    assert resumed.completion_count == 3
    assert resumed.update_count == 2


@pytest.mark.parametrize(
    ("failure", "expected_status"),
    (
        (
            _ExecutorFailure(
                code="EXECUTOR_OUTPUT_INVALID",
                completion_observed=True,
                executor_stage="COMPLETION_VALIDATION",
            ),
            TaskwiseRunStatusV1.EXECUTION_FAILED,
        ),
        (
            _ExecutorFailure(
                code="EXECUTOR_SECURITY_TOOL_USE_VIOLATION",
                completion_observed=False,
                event_digest=_sha256("security event stream"),
                event_counts={"file_read": 1},
                executor_stage="EVENT_STREAM_PARSE",
            ),
            TaskwiseRunStatusV1.SECURITY_TOOL_USE_VIOLATION,
        ),
    ),
)
def test_completion_or_security_failure_forbids_retry_and_resume(
    tmp_path: Path,
    failure: _ExecutorFailure,
    expected_status: TaskwiseRunStatusV1,
) -> None:
    config = _config(tmp_path, "control")
    executor = _DummyExecutor([failure, "A"])
    failed = TaskwiseOnlineRunnerV1(
        config=config,
        episodes=_episodes(1),
        executor=executor,
        session_id_factory=_session_factory("terminal"),
    ).run()

    assert failed.status is expected_status
    assert failed.resume_allowed is False
    assert len(executor.requests) == 1
    failure_rows = [
        json.loads(line)
        for line in (config.output_directory / "private" / "failures.jsonl")
        .read_text()
        .splitlines()
    ]
    assert len(failure_rows) == 1
    assert failure_rows[0]["code"] == failure.taskwise_failure_code
    assert failure_rows[0]["completion_observed"] is failure.completion_observed
    with pytest.raises(RuntimeError, match="forbids resume"):
        TaskwiseOnlineRunnerV1(
            config=config,
            episodes=_episodes(1),
            executor=_DummyExecutor(["A"] * 3),
            resume=True,
            session_id_factory=_session_factory("forbidden"),
        ).run()


@pytest.mark.parametrize("code", sorted(TASKWISE_EXECUTOR_FAILURE_CODES_V1))
def test_runner_preserves_every_closed_executor_failure_code(
    tmp_path: Path,
    code: str,
) -> None:
    executor = _DummyExecutor(
        [
            _ExecutorFailure(
                code=code,
                completion_observed=False,
                executor_stage="PRE_INVOCATION_ATTESTATION",
            )
        ]
    )

    result = TaskwiseOnlineRunnerV1(
        config=_config(tmp_path / code, "control"),
        episodes=_episodes(1),
        executor=executor,
        session_id_factory=_session_factory(f"closed-{code.lower()}"),
    ).run()

    expected_status = (
        TaskwiseRunStatusV1.SECURITY_TOOL_USE_VIOLATION
        if code == "EXECUTOR_SECURITY_TOOL_USE_VIOLATION"
        else TaskwiseRunStatusV1.EXECUTION_FAILED
    )
    assert result.status is expected_status
    assert result.finding_codes == (code,)
    assert result.resume_allowed is False


def test_nonempty_security_event_counts_override_nonsecurity_executor_code(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, "control")
    result = TaskwiseOnlineRunnerV1(
        config=config,
        episodes=_episodes(1),
        executor=_DummyExecutor(
            [
                _ExecutorFailure(
                    code="EXECUTOR_MODEL_TRANSPORT_FAILED",
                    completion_observed=False,
                    event_digest=_sha256("contradictory security event stream"),
                    event_counts={"file_read": 1},
                    executor_stage="MODEL_TRANSPORT",
                )
            ]
        ),
        session_id_factory=_session_factory("security-count-priority"),
    ).run()

    assert result.status is TaskwiseRunStatusV1.SECURITY_TOOL_USE_VIOLATION
    assert result.finding_codes == ("EXECUTOR_SECURITY_TOOL_USE_VIOLATION",)
    assert result.resume_allowed is False
    state = json.loads((config.output_directory / "run_state.json").read_text())
    assert state["failure"]["code"] == "EXECUTOR_SECURITY_TOOL_USE_VIOLATION"
    failure = json.loads((config.output_directory / "private" / "failures.jsonl").read_text())
    assert failure["security_violation"] is True
    assert failure["event_counts"] == {"file_read": 1}


def test_unknown_executor_failure_collapses_without_exception_body(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, "control")
    executor = _DummyExecutor(
        [
            _ExecutorFailure(
                code="NOT_IN_THE_CLOSED_VOCABULARY",
                completion_observed=False,
                diagnostic_receipt="../../unsafe-private-body",
                executor_stage="UNSAFE_STAGE",
            )
        ]
    )

    result = TaskwiseOnlineRunnerV1(
        config=config,
        episodes=_episodes(1),
        executor=executor,
        session_id_factory=_session_factory("unknown-executor"),
    ).run()

    assert result.finding_codes == ("EXECUTOR_INTERNAL_ERROR",)
    state_text = (config.output_directory / "run_state.json").read_text()
    failures_text = (config.output_directory / "private" / "failures.jsonl").read_text()
    assert "NOT_IN_THE_CLOSED_VOCABULARY" not in state_text + failures_text
    assert "PRIVATE_EXECUTOR_BODY_SENTINEL" not in state_text + failures_text
    assert "../../unsafe-private-body" not in state_text + failures_text
    row = json.loads(failures_text)
    assert row["code"] == "EXECUTOR_INTERNAL_ERROR"
    assert row["completion_observed"] is False
    assert row["executor_stage"] == "INTERNAL"
    assert row["diagnostic_receipt"] is None


def test_reflector_security_violation_terminates_the_whole_online_run(
    tmp_path: Path,
) -> None:
    executor = _DummyExecutor(["B", "A", "A"])
    result = TaskwiseOnlineRunnerV1(
        config=_config(tmp_path, "online"),
        episodes=_episodes(1),
        executor=executor,
        core_update_port=_FailingCorePort(),
        session_id_factory=_session_factory("reflector-security"),
    ).run()

    assert result.status is TaskwiseRunStatusV1.SECURITY_TOOL_USE_VIOLATION
    assert result.resume_allowed is False
    assert result.completion_count == 1
    assert result.update_count == 0
    assert len(executor.requests) == 1
    assert result.finding_codes == ("EXECUTOR_SECURITY_TOOL_USE_VIOLATION",)
    with pytest.raises(RuntimeError, match="forbids resume"):
        TaskwiseOnlineRunnerV1(
            config=_config(tmp_path, "online"),
            episodes=_episodes(1),
            executor=_DummyExecutor(["A"] * 3),
            core_update_port=_DummyCorePort(),
            resume=True,
            session_id_factory=_session_factory("reflector-resume-forbidden"),
        ).run()


def test_private_diagnostics_failure_cannot_erase_security_latch(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, "control")
    result = _PrivateFailurePersistenceFailingRunner(
        config=config,
        episodes=_episodes(1),
        executor=_DummyExecutor(
            [
                _ExecutorFailure(
                    code="EXECUTOR_SECURITY_TOOL_USE_VIOLATION",
                    completion_observed=False,
                    event_digest=_sha256("latched security event stream"),
                    event_counts={"mcp": 1},
                    executor_stage="EVENT_STREAM_PARSE",
                )
            ]
        ),
        session_id_factory=_session_factory("security-diagnostic-failure"),
    ).run()

    assert result.status is TaskwiseRunStatusV1.SECURITY_TOOL_USE_VIOLATION
    assert result.resume_allowed is False
    assert result.finding_codes == (
        "EXECUTOR_SECURITY_TOOL_USE_VIOLATION",
        "EXECUTOR_PRIVATE_DIAGNOSTICS_FAILED",
    )
    state = json.loads((config.output_directory / "run_state.json").read_text())
    assert state["failure"]["code"] == "EXECUTOR_SECURITY_TOOL_USE_VIOLATION"
    assert state["failure"]["secondary_finding_codes"] == ["EXECUTOR_PRIVATE_DIAGNOSTICS_FAILED"]
    serialized = json.dumps(state, sort_keys=True)
    assert "PRIVATE_DIAGNOSTIC_PERSISTENCE_BODY_SENTINEL" not in serialized
    assert (config.output_directory / "private" / "failures.jsonl").read_bytes() == b""
    with pytest.raises(RuntimeError, match="forbids resume"):
        TaskwiseOnlineRunnerV1(
            config=config,
            episodes=_episodes(1),
            executor=_DummyExecutor(["A"] * 3),
            resume=True,
            session_id_factory=_session_factory("security-no-resume"),
        ).run()


def test_unresolvable_approved_memory_fails_without_predecessor_fallback(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, "online")
    executor = _DummyExecutor(["B", "A", "A"])
    core = _UnresolvableCorePort()

    result = TaskwiseOnlineRunnerV1(
        config=config,
        episodes=_episodes(1),
        executor=executor,
        core_update_port=core,
        session_id_factory=_session_factory("no-fallback"),
    ).run()

    assert result.status is TaskwiseRunStatusV1.TASKWISE_EVOLUTION_UPDATE_FAILED
    assert result.finding_codes == ("TASKWISE_EVOLUTION_UPDATE_FAILED",)
    assert result.resume_allowed is False
    assert result.completion_count == 1
    assert result.update_count == 1
    assert result.memory_aggregate.approved_artifact_count == 1
    assert len(core.requests) == 1
    assert len(executor.requests) == 1
    state = json.loads((config.output_directory / "run_state.json").read_text())
    assert state["failure"]["code"] == "TASKWISE_EVOLUTION_UPDATE_FAILED"
    assert state["failure"]["completion_observed"] is True
    assert len(state["failure"]["diagnostic_record_sha256"]) == 64
    assert state["active_memory"]["core_artifact_id"] == "artifact_0_1"
    with pytest.raises(RuntimeError, match="forbids resume"):
        TaskwiseOnlineRunnerV1(
            config=config,
            episodes=_episodes(1),
            executor=_DummyExecutor(["A"] * 2),
            core_update_port=core,
            resume=True,
            session_id_factory=_session_factory("no-fallback-resume"),
        ).run()


def test_update_two_failure_does_not_start_round_two_or_fallback(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, "online")
    executor = _DummyExecutor(["B", "B", "A"])
    core = _UpdateTwoFailingCorePort()

    result = TaskwiseOnlineRunnerV1(
        config=config,
        episodes=_episodes(1),
        executor=executor,
        core_update_port=core,
        session_id_factory=_session_factory("update-two-failure"),
    ).run()

    assert result.status is TaskwiseRunStatusV1.TASKWISE_EVOLUTION_UPDATE_FAILED
    assert result.finding_codes == ("TASKWISE_EVOLUTION_UPDATE_FAILED",)
    assert result.resume_allowed is False
    assert result.completion_count == 2
    assert result.update_count == 1
    assert result.session_attempt_count == 2
    assert [request.round_index for request in executor.requests] == [0, 1]
    assert executor.requests[0].resolved_text_memory is None
    assert executor.requests[1].resolved_text_memory.core_artifact_id == "artifact_0_1"
    assert [request.update_index for request in core.requests] == [1, 2]
    state = json.loads((config.output_directory / "run_state.json").read_text())
    assert state["episode_state"] == TaskwiseEpisodeStateV1.ROUND_1_COMPLETED.value
    assert state["active_memory"]["core_artifact_id"] == "artifact_0_1"
    assert state["failure"]["code"] == "TASKWISE_EVOLUTION_UPDATE_FAILED"
    assert state["failure"]["completion_observed"] is True
    assert len(state["failure"]["diagnostic_record_sha256"]) == 64
    failure_rows = [
        json.loads(line)
        for line in (config.output_directory / "private" / "failures.jsonl")
        .read_text()
        .splitlines()
    ]
    assert len(failure_rows) == 1
    assert failure_rows[0]["code"] == "TASKWISE_EVOLUTION_UPDATE_FAILED"
    assert failure_rows[0]["update_index"] == 2
    assert failure_rows[0]["round_index"] == 1
    assert failure_rows[0]["session_id"] is None
    assert "synthetic update-two validation failure" not in json.dumps(failure_rows)
    with pytest.raises(RuntimeError, match="forbids resume"):
        TaskwiseOnlineRunnerV1(
            config=config,
            episodes=_episodes(1),
            executor=_DummyExecutor(["A"]),
            core_update_port=core,
            resume=True,
            session_id_factory=_session_factory("update-two-resume-forbidden"),
        ).run()


def test_resume_rejects_binding_or_history_tampering(tmp_path: Path) -> None:
    config = _config(tmp_path, "control")
    with pytest.raises(_SimulatedProcessLoss):
        _PauseAfterStateRunner(
            config=config,
            episodes=_episodes(1),
            executor=_DummyExecutor(["A"]),
            pause_state=TaskwiseEpisodeStateV1.TASK_OPENED,
            session_id_factory=_session_factory("tamper"),
        ).run()
    state_path = config.output_directory / "run_state.json"
    state = json.loads(state_path.read_text())
    state["binding"]["task_sequence_sha256"] = "0" * 64
    state_path.write_text(
        json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="binding mismatch"):
        TaskwiseOnlineRunnerV1(
            config=config,
            episodes=_episodes(1),
            executor=_DummyExecutor(["A"] * 3),
            resume=True,
            session_id_factory=_session_factory("tampered"),
        ).run()


def test_resume_recomputes_persisted_context_binding_even_with_valid_chain(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, "control")
    with pytest.raises(_SimulatedProcessLoss):
        _PauseAfterStateRunner(
            config=config,
            episodes=_episodes(1),
            executor=_DummyExecutor(["A"]),
            pause_state=TaskwiseEpisodeStateV1.ROUND_0_COMPLETED,
            session_id_factory=_session_factory("receipt-tamper"),
        ).run()

    public_path = config.output_directory / "public" / "events.jsonl"
    rows = [json.loads(line) for line in public_path.read_text().splitlines()]
    original = rows[0]["context_binding"]
    expected = TaskwiseSessionContextBindingV1(
        session_id=original["expected_session_id"],
        core_artifact_id=None,
        artifact_payload_sha256=None,
        resolved_memory_sha256=None,
        context_resolution_digest=None,
    )
    actual = TaskwiseSessionContextBindingV1(
        session_id="session_tampered_9999",
        core_artifact_id=None,
        artifact_payload_sha256=None,
        resolved_memory_sha256=None,
        context_resolution_digest=None,
    )
    rows[0]["context_binding"] = issue_taskwise_context_binding_receipt_v1(
        expected=expected,
        actual=actual,
    ).to_public_dict()
    public_path.write_text(
        "".join(
            json.dumps(
                row,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )
    state_path = config.output_directory / "run_state.json"
    state = json.loads(state_path.read_text())
    state["public_event_chain_sha256"] = _chain_rows(rows)
    state_path.write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="context-binding receipt mismatch"):
        TaskwiseOnlineRunnerV1(
            config=config,
            episodes=_episodes(1),
            executor=_DummyExecutor(["A", "A"]),
            resume=True,
            session_id_factory=_session_factory("receipt-rejected"),
        ).run()
