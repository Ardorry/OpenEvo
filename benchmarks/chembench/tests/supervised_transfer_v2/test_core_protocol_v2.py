from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from zipfile import ZIP_DEFLATED, ZipFile

import openevo.evolution.methods as core_methods
import pytest
from openevo import __version__
from openevo.evolution.framework import DistributionArtifactExpectation
from openevo.evolution.framework import builtins as core_builtins
from openevo.evolution.framework.builtins import load_verified_builtin_registry
from openevo.evolution.framework.loading import _verify_distribution_install

from openevo_chembench.chembench4k_evaluation import ChemBench4KPrivateEvaluator
from openevo_chembench.chembench4k_models import (
    CHEMBENCH4K_REVISION,
    PrivateChemBench4KTask,
    RenderedChemBench4KPrompt,
)
from openevo_chembench.frozen_runtime_v2 import _issue_core_resolved_text_memory_v2
from openevo_chembench.models import RawAttempt, TranscriptReference
from openevo_chembench.supervised_transfer_v2.artifacts import (
    CoreResolvedSupervisedContextV2,
    _issue_core_resolved_auxiliary_v2,
)
from openevo_chembench.supervised_transfer_v2.config import (
    SupervisedTransferConfigV2,
    load_config_v2,
)
from openevo_chembench.supervised_transfer_v2.context_binding import (
    SupervisedAgentRequestV2,
    SupervisedSessionContextBindingV2,
    issue_supervised_context_binding_receipt_v2,
)
from openevo_chembench.supervised_transfer_v2.core import (
    TaskwiseCoreEvolutionBridgeV1,
    TaskwiseCoreUpdateRequestV1,
    _trajectory_forbidden_literals,
)
from openevo_chembench.supervised_transfer_v2.executor import (
    SupervisedTaskExecutionCodeV2,
    SupervisedTaskExecutionErrorV2,
)
from openevo_chembench.supervised_transfer_v2.experiment import (
    FrozenThreeTargetSetV2,
    SupervisedTransferExperimentV2,
    load_experiment_inputs_v2,
)
from openevo_chembench.supervised_transfer_v2.memory import (
    SUPERVISED_MEMORY_LIMITS_V2,
)
from openevo_chembench.supervised_transfer_v2.packet import (
    SupervisedEvolutionPacketV2,
)
from openevo_chembench.supervised_transfer_v2.trajectory import (
    SupervisedTrajectoryV2,
)
from openevo_chembench.taskwise_feedback_v1 import (
    safe_signal_from_private_evaluation,
)


class _SourceDistribution:
    metadata: ClassVar[dict[str, str]] = {"Name": "openevo"}
    version = __version__

    def __init__(self, install_root: Path) -> None:
        self._install_root = install_root

    def locate_file(self, path: str) -> Path:
        return self._install_root / path

    def read_text(self, _filename: str) -> None:
        return None


def _registry(root: Path):
    install_root = Path(core_builtins.__file__).resolve().parents[3]
    artifact = root / f"openevo-{__version__}-py3-none-any.whl"
    root.mkdir()
    with ZipFile(artifact, "w", compression=ZIP_DEFLATED) as wheel:
        for path in sorted((install_root / "openevo").rglob("*")):
            if path.is_file() and path.name.endswith(
                (".py", ".pyi", ".so", ".pyd", ".dll", ".dylib")
            ):
                wheel.write(path, path.relative_to(install_root).as_posix())
        wheel.writestr(
            f"openevo-{__version__}.dist-info/METADATA",
            f"Name: openevo\nVersion: {__version__}\n",
        )
    verified = _verify_distribution_install(
        DistributionArtifactExpectation(
            distribution="openevo",
            distribution_version=__version__,
            distribution_digest=hashlib.sha256(artifact.read_bytes()).hexdigest(),
        ),
        artifact,
        metadata_provider=lambda _name: _SourceDistribution(install_root),
    )
    return load_verified_builtin_registry(verified)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _synthetic_runtime_services() -> SimpleNamespace:
    return SimpleNamespace(
        digest=_sha("runtime-services"),
        service_run_id="stv2-services-20990101T000000Z-deadbeef",
    )


def _task(index: int) -> PrivateChemBench4KTask:
    return PrivateChemBench4KTask(
        uid=_sha(f"v2-task-{index}"),
        category="Name_Conversion",
        source_split="test",
        source_index=index,
        question=f"synthetic question marker {index}",
        A="synthetic alpha",
        B="synthetic beta",
        C="synthetic gamma",
        D="synthetic delta",
        target="A",
        dataset_revision=CHEMBENCH4K_REVISION,
        dataset_sha256=_sha("synthetic-dataset"),
    )


def _prompt(task: PrivateChemBench4KTask) -> RenderedChemBench4KPrompt:
    return RenderedChemBench4KPrompt(
        uid=task.uid,
        category=task.category,
        dataset_revision=task.dataset_revision,
        demonstration_uids=tuple(_sha(f"demo-{index}") for index in range(5)),
        text=(
            "There is a single choice question about chemistry.\n"
            f"Question: {task.question}\n"
            f"A. {task.A}\nB. {task.B}\nC. {task.C}\nD. {task.D}\nAnswer:"
        ),
    )


def _attempt(
    task: PrivateChemBench4KTask,
    *,
    task_index: int,
    round_index: int,
    response: str,
) -> tuple[SupervisedTrajectoryV2, object]:
    evaluation = ChemBench4KPrivateEvaluator().evaluate(
        task=task,
        raw_completion=response,
    )
    trajectory = SupervisedTrajectoryV2.from_attempt(
        task_uid=task.uid,
        task_index=task_index,
        category=task.category,
        round_index=round_index,
        session_id=f"supervised-v2-session-{task_index}-{round_index}",
        prompt=_prompt(task),
        attempt=RawAttempt(
            response=response,
            transcript_reference=TranscriptReference(
                f"synthetic-transcript-{task_index}-{round_index}"
            ),
        ),
        safe_feedback=safe_signal_from_private_evaluation(evaluation),
        dataset_sha256=task.dataset_sha256,
    )
    return trajectory, evaluation


def _memory(evidence_digest: str) -> str:
    return f"""# Category Memory: Name_Conversion

## Confirmed Principles
- None.

## Provisional Principles
- Rule ID=NC-P-001; Status=provisional; Category=Name_Conversion; Target Type=text_memory; Trigger=an unambiguous molecular formula; Principle=apply deterministic nomenclature constraints; Action=enumerate compatible functional groups before naming; Validation=round-trip the proposed name to the formula; Evidence Count=1; Supporting Train Ordinals Hash={_sha("ordinal-1")}; First Seen Cycle=1; Last Confirmed Cycle=0; Contradiction Count=0; Evidence=current Train observation; Evidence Digests={evidence_digest}

## Common Failure Modes
- Treating a plausible synonym as unique without a round-trip check.

## Option Elimination Checks
- Reject choices whose locants contradict functional-group precedence.

## Retired Or Contradicted
- None.

## Output Discipline
- Return one uppercase choice letter and no explanation.

## Do
- Apply a category principle only when its trigger holds.

## Avoid
- Avoid instance-specific answer mappings.

## Validate
- Validate structure, locants, and final answer format.

## When Applicable
- Use only rules whose trigger matches the current item.

## Retired Or Superseded
- Ignore retired rules above.
"""


def test_three_cycles_create_nine_jobs_and_carry_all_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_packet = [""]
    monkeypatch.setattr(
        core_methods,
        "_generate_reflector_markdown",
        lambda *_args, **_kwargs: _memory(active_packet[-1]),
    )
    bridge = TaskwiseCoreEvolutionBridgeV1(
        db_path=tmp_path / "core/evolution.sqlite3",
        artifact_root=tmp_path / "core/artifacts",
        executable_registry=_registry(tmp_path / "registry"),
        checkpoint_path=tmp_path / "core/private-checkpoints.jsonl",
        memory_limits=SUPERVISED_MEMORY_LIMITS_V2,
    )
    try:
        task = _task(0)
        trajectories = []
        evaluations = []
        head = None
        context = None
        for round_index, response in enumerate(("B", "A", "A")):
            trajectory, evaluation = _attempt(
                task,
                task_index=0,
                round_index=round_index,
                response=response,
            )
            trajectories.append(trajectory)
            evaluations.append(evaluation)
            packet = SupervisedEvolutionPacketV2.from_evaluations(
                task=task,
                training_task_ordinal=1,
                round_index=round_index,
                evaluations=tuple(evaluations),
                trajectory_ids=tuple(value.trajectory_id for value in trajectories),
                predecessor_memory=None if context is None else context.memory.markdown,
                predecessor_artifact_id=(
                    None if context is None else context.memory.core_artifact_id
                ),
                predecessor_skill=None if context is None else context.skill.markdown,
                predecessor_skill_artifact_id=(
                    None if context is None else context.skill.core_artifact_id
                ),
                predecessor_agent_system=(
                    None if context is None else context.agent_system.markdown
                ),
                predecessor_agent_system_artifact_id=(
                    None if context is None else context.agent_system.core_artifact_id
                ),
            )
            active_packet.append(packet.digest)
            head = bridge.apply_update(
                TaskwiseCoreUpdateRequestV1(
                    task_uid=task.uid,
                    task_index=0,
                    round_index=round_index,
                    update_index=round_index + 1,
                    trajectories=tuple(trajectories),
                    predecessor=None if head is None else head.predecessor_identity(),
                    validator_forbidden_literals=tuple(
                        literal
                        for value in trajectories
                        for literal in _trajectory_forbidden_literals(value)
                    ),
                    supervised_packet=packet,
                ),
                test_only_allow_synthetic_reflector=True,
            )
            context = bridge.issue_runtime_context(head)
            assert [value.target_id for value in head.supervised_auxiliary_artifacts] == [
                "skill_bundle",
                "agent_system",
            ]
        assert head is not None and head.global_update_ordinal == 3
        next_task = _task(1)
        trajectory, evaluation = _attempt(
            next_task,
            task_index=1,
            round_index=0,
            response="B",
        )
        packet = SupervisedEvolutionPacketV2.from_evaluations(
            task=next_task,
            training_task_ordinal=2,
            round_index=0,
            evaluations=(evaluation,),
            trajectory_ids=(trajectory.trajectory_id,),
            predecessor_memory=context.memory.markdown,
            predecessor_artifact_id=context.memory.core_artifact_id,
            predecessor_skill=context.skill.markdown,
            predecessor_skill_artifact_id=context.skill.core_artifact_id,
            predecessor_agent_system=context.agent_system.markdown,
            predecessor_agent_system_artifact_id=context.agent_system.core_artifact_id,
        )
        active_packet.append(packet.digest)
        successor = bridge.apply_update(
            TaskwiseCoreUpdateRequestV1(
                task_uid=next_task.uid,
                task_index=1,
                round_index=0,
                update_index=1,
                trajectories=(trajectory,),
                predecessor=head.predecessor_identity(),
                validator_forbidden_literals=_trajectory_forbidden_literals(trajectory),
                supervised_packet=packet,
            ),
            test_only_allow_synthetic_reflector=True,
        )
        bridge.issue_runtime_context(successor)
        assert successor.global_update_ordinal == 4
        with bridge._store.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 12
            assert connection.execute("SELECT COUNT(*) FROM contexts").fetchone()[0] == 12
            counts = dict(
                connection.execute(
                    "SELECT type, COUNT(*) FROM artifacts "
                    "WHERE type IN ('text_memory','skill_bundle','agent_system') "
                    "GROUP BY type"
                ).fetchall()
            )
        assert counts == {"agent_system": 4, "skill_bundle": 4, "text_memory": 4}
    finally:
        bridge.close()


class _SyntheticBridge:
    def __init__(self, bridge: TaskwiseCoreEvolutionBridgeV1, active: list[str]) -> None:
        self.bridge = bridge
        self.active = active

    def apply_update(self, request):
        self.active.append(request.supervised_packet.digest)
        return self.bridge.apply_update(
            request,
            test_only_allow_synthetic_reflector=True,
        )

    def issue_runtime_context(self, result):
        return self.bridge.issue_runtime_context(result)


class _FakeExecutor:
    def __init__(self) -> None:
        self.requests = []
        self.receipts = {}

    def execute(self, request):
        self.requests.append(request)
        binding = SupervisedSessionContextBindingV2.from_context(
            session_id=request.session_id,
            context=request.resolved_context,
        )
        self.receipts[request.session_id] = issue_supervised_context_binding_receipt_v2(
            expected=binding,
            actual=binding,
        )
        return RawAttempt(
            response="A",
            transcript_reference=TranscriptReference(f"synthetic:{request.session_id}"),
        )

    def consume_context_receipt(self, session_id):
        return self.receipts.pop(session_id)


class _RetryOnceExecutor(_FakeExecutor):
    def __init__(self, *, fail_once: bool) -> None:
        super().__init__()
        self.fail_once = fail_once

    def execute(self, request: SupervisedAgentRequestV2) -> RawAttempt:
        if self.fail_once:
            self.fail_once = False
            self.requests.append(request)
            raise SupervisedTaskExecutionErrorV2(
                SupervisedTaskExecutionCodeV2.TASK_FAILED,
                completion_exists=False,
            )
        return super().execute(request)


def _resolved_context() -> CoreResolvedSupervisedContextV2:
    memory = "# Category Memory: Name_Conversion\n\n## Output Discipline\n- answer once\n"
    skill = "# Category Skill: Name_Conversion\n\n## Workflow\n- validate naming\n"
    system = "# Category Agent System: Name_Conversion\n\n## Directives\n- answer once\n"
    return CoreResolvedSupervisedContextV2(
        memory=_issue_core_resolved_text_memory_v2(
            core_artifact_id="memory-artifact-v2",
            artifact_payload_sha256=_sha(memory),
            context_resolution_digest="1" * 64,
            resolved_memory_sha256=_sha(memory),
            markdown=memory,
        ),
        skill=_issue_core_resolved_auxiliary_v2(
            target_id="skill_bundle",
            core_artifact_id="skill-artifact-v2",
            artifact_payload_sha256=_sha(skill),
            context_resolution_digest="2" * 64,
            resolved_content_sha256=_sha(skill),
            markdown=skill,
        ),
        agent_system=_issue_core_resolved_auxiliary_v2(
            target_id="agent_system",
            core_artifact_id="system-artifact-v2",
            artifact_payload_sha256=_sha(system),
            context_resolution_digest="3" * 64,
            resolved_content_sha256=_sha(system),
            markdown=system,
        ),
    )


def test_controller_runs_four_sessions_three_cycles_without_model_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = Path(__file__).resolve().parents[4]
    config_path = (
        repository
        / "benchmarks/chembench/configs/supervised_transfer_v2/chembench_supervised_transfer_v2.yaml"
    )
    config = load_config_v2(config_path.resolve())
    payload = dict(config.payload)
    payload["roots"] = {
        "results": str(tmp_path / "results"),
        "state": str(tmp_path / "state"),
        "reports": str(tmp_path / "reports"),
    }
    test_config = SupervisedTransferConfigV2(path=config.path, payload=payload)
    loaded = load_experiment_inputs_v2(
        repository,
        config,
        require_reflector_runtime=False,
    )
    inputs = replace(
        loaded,
        config=test_config,
        managed_codex=SimpleNamespace(
            digest=_sha("managed-codex"),
            executable_sha256=_sha("managed-binary"),
            executable=tmp_path / "unused-codex",
        ),
            candidate_codex=SimpleNamespace(
                digest=_sha("candidate-codex"),
                executable_sha256=_sha("candidate-binary"),
            ),
            runtime_services=_synthetic_runtime_services(),
    )
    monkeypatch.setattr(
        "openevo_chembench.supervised_transfer_v2.experiment._benchmark_status",
        lambda _repository: "",
    )
    active = [""]
    monkeypatch.setattr(
        core_methods,
        "_generate_reflector_markdown",
        lambda *_args, **_kwargs: _memory(active[-1]),
    )
    bridge = TaskwiseCoreEvolutionBridgeV1(
        db_path=tmp_path / "controller-core/evolution.sqlite3",
        artifact_root=tmp_path / "controller-core/artifacts",
        executable_registry=_registry(tmp_path / "controller-registry"),
        checkpoint_path=tmp_path / "controller-core/checkpoints.jsonl",
        memory_limits=SUPERVISED_MEMORY_LIMITS_V2,
    )
    executor = _FakeExecutor()
    experiment = SupervisedTransferExperimentV2(
        inputs=inputs,
        run_id="stv2-synthetic-controller-0001",
        run_mode="preflight",
        executor_factory=lambda _arm: executor,
        bridge_factory=lambda _root, _category: _SyntheticBridge(bridge, active),
    )
    experiment._state["stage"] = "ONLINE_TRAIN"
    experiment._write_state()
    task = next(value for value in inputs.train if value.category == "Name_Conversion")
    try:
        head = experiment._run_one_online_task(
            executor,
            task=task,
            category_ordinal=0,
            bridge=_SyntheticBridge(bridge, active),
            stage="ONLINE_TRAIN",
            persist_head=True,
        )
        assert head.global_update_ordinal == 3
        assert len(executor.requests) == 4
        assert [request.round_index for request in executor.requests] == [0, 1, 2, 3]
        assert executor.requests[0].resolved_context is None
        assert all(request.resolved_context is not None for request in executor.requests[1:])
        assert experiment._state["task_sessions"] == 4
        assert experiment._state["reflector_calls"] == 3
        assert experiment._state["core_jobs"] == 9
        with bridge._store.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 9
    finally:
        bridge.close()


def test_final_test_retries_only_no_completion_and_uses_protocol_global_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = Path(__file__).resolve().parents[4]
    config_path = (
        repository
        / "benchmarks/chembench/configs/supervised_transfer_v2/chembench_supervised_transfer_v2.yaml"
    )
    config = load_config_v2(config_path.resolve())
    payload = dict(config.payload)
    payload["roots"] = {
        "results": str(tmp_path / "results"),
        "state": str(tmp_path / "state"),
        "reports": str(tmp_path / "reports"),
    }
    loaded = load_experiment_inputs_v2(
        repository,
        config,
        require_reflector_runtime=False,
    )
    task = next(value for value in loaded.test if value.category == "Name_Conversion")
    inputs = replace(
        loaded,
        config=SupervisedTransferConfigV2(path=config.path, payload=payload),
        test=(task,),
        managed_codex=SimpleNamespace(
            digest=_sha("managed-codex"),
            executable_sha256=_sha("managed-binary"),
            executable=tmp_path / "unused-codex",
        ),
            candidate_codex=SimpleNamespace(
                digest=_sha("candidate-codex"),
                executable_sha256=_sha("candidate-binary"),
            ),
            runtime_services=_synthetic_runtime_services(),
    )
    monkeypatch.setattr(
        "openevo_chembench.supervised_transfer_v2.experiment._benchmark_status",
        lambda _repository: "",
    )
    monkeypatch.setattr(
        "openevo_chembench.supervised_transfer_v2.experiment.TEST_COUNT",
        1,
    )
    monkeypatch.setattr(
        "openevo_chembench.supervised_transfer_v2.experiment.time.sleep",
        lambda _seconds: None,
    )
    monkeypatch.setattr(
        SupervisedTransferExperimentV2,
        "_require_source_frozen",
        lambda _self: None,
    )
    experiment = SupervisedTransferExperimentV2(
        inputs=inputs,
        run_id="stv2-synthetic-test-ledger-0001",
        run_mode="preflight",
        executor_factory=lambda _arm: _FakeExecutor(),
        bridge_factory=lambda _root, _category: None,  # type: ignore[arg-type,return-value]
    )
    experiment._state["stage"] = "FREEZE_THREE_TARGETS"
    experiment._write_state()
    control = _RetryOnceExecutor(fail_once=True)
    evolved = _RetryOnceExecutor(fail_once=False)
    context = _resolved_context()
    frozen = FrozenThreeTargetSetV2(
        contexts={"Name_Conversion": context},
        artifact_set_digest=_sha("frozen-artifact-set"),
        receipt_sha256=_sha("frozen-receipt"),
    )

    experiment._run_final_test(frozen, control, evolved)

    assert len(control.requests) == 2
    assert len(evolved.requests) == 1
    assert control.requests[0].session_id != control.requests[1].session_id
    global_ledger = tmp_path / "state/private/final_test_consumption_ledger_v2.jsonl"
    assert global_ledger.is_file()
    assert not (experiment.state_root / "private/final_test_consumption_ledger_v2.jsonl").exists()
    rows = [json.loads(line) for line in global_ledger.read_text().splitlines()]
    assert sum(row["completion_exists"] for row in rows) == 2
    assert sum(not row["completion_exists"] for row in rows) == 3
