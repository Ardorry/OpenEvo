from __future__ import annotations

import hashlib
import json
import sqlite3
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

import openevo_chembench.supervised_transfer_v2.core as supervised_core
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
    inspect_supervised_auxiliary_artifact_v2,
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
    TaskwiseCoreEvolutionError,
    TaskwiseCoreUpdateRequestV1,
    _bind_auxiliary_source_evidence,
    _contains_answer_map,
    _decode_validator_literal,
    _trajectory_forbidden_literals,
)
from openevo_chembench.supervised_transfer_v2.executor import (
    SupervisedTaskExecutionCodeV2,
    SupervisedTaskExecutionErrorV2,
)
from openevo_chembench.supervised_transfer_v2.experiment import (
    FrozenThreeTargetSetV2,
    SupervisedExperimentV2Error,
    SupervisedTransferExperimentV2,
    load_experiment_inputs_v2,
    paid_cli_run_mode_v2,
)
from openevo_chembench.supervised_transfer_v2.memory import (
    SUPERVISED_MEMORY_LIMITS_V2,
)
from openevo_chembench.supervised_transfer_v2.packet import (
    SupervisedEvolutionPacketV2,
)
from openevo_chembench.supervised_transfer_v2.reflector_boundary import (
    _SUPERVISED_OUTPUT_SCHEMA,
    ReflectorBoundaryError,
    _render_supervised_structured_auxiliary,
    _render_supervised_structured_memory,
    _supporting_task_set_hash,
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


def test_paid_cli_command_maps_to_closed_controller_mode() -> None:
    assert paid_cli_run_mode_v2("contract-canary") == "contract_canary"
    assert paid_cli_run_mode_v2("preflight") == "preflight"
    assert paid_cli_run_mode_v2("formal") == "formal"
    with pytest.raises(ValueError, match="invalid paid CLI command"):
        paid_cli_run_mode_v2("contract_canary")


def test_structured_skill_renderer_produces_validator_clean_artifact() -> None:
    evidence = "1" * 64
    response = json.dumps(
        {
            "category": "Yield_Prediction",
            "confirmed_principles": [],
            "provisional_principles": [],
            "common_failure_modes": [],
            "option_elimination_checks": [],
            "retired_or_contradicted": [],
            "output_discipline": ["Return one uppercase option."],
            "do": ["Check physical bounds."],
            "avoid": ["Avoid unsupported estimates."],
            "validate": ["Verify the limiting amount."],
            "when_applicable": ["Use for yield estimates."],
            "retired_or_superseded": [],
            "skill_when_to_use": ["When a yield estimate is requested."],
            "skill_workflow": ["Apply mass-balance bounds before comparing choices."],
            "skill_validation_checks": ["Verify the estimate is physically bounded."],
            "skill_failure_guards": ["Reject values above full conversion."],
            "skill_evidence_digests": [evidence],
            "agent_system_directives": [
                {
                    "trigger": "solving a yield prediction",
                    "instruction": "apply explicit mass-balance constraints",
                    "validation": "check physical bounds before choosing",
                    "evidence_digests": [evidence],
                }
            ],
            "agent_system_output_discipline": ["Return one uppercase option."],
        },
        sort_keys=True,
        separators=(",", ":"),
    )

    auxiliary = _render_supervised_structured_auxiliary(response)
    inspection = inspect_supervised_auxiliary_artifact_v2(
        auxiliary.skill_markdown.encode("utf-8"),
        target_id="skill_bundle",
        category="Yield_Prediction",
        source_evidence_digests=auxiliary.skill_evidence_digests,
        allowed_evidence_digests=frozenset({evidence}),
        forbidden_literals=(),
    )

    assert inspection.passed
    assert "chembench" not in auxiliary.skill_markdown.casefold()


def test_structured_output_schema_requires_nonempty_auxiliary_targets() -> None:
    properties = _SUPERVISED_OUTPUT_SCHEMA["properties"]

    for field in (
        "skill_when_to_use",
        "skill_workflow",
        "skill_validation_checks",
        "skill_failure_guards",
        "agent_system_output_discipline",
        "agent_system_directives",
    ):
        assert properties[field]["minItems"] == 1

    for field in (
        "confirmed_principles",
        "provisional_principles",
        "common_failure_modes",
        "option_elimination_checks",
        "retired_or_contradicted",
        "output_discipline",
        "do",
        "avoid",
        "validate",
        "when_applicable",
        "retired_or_superseded",
    ):
        assert "minItems" not in properties[field]


def test_structured_memory_renderer_owns_supporting_hash_and_scrubs_answer_map() -> None:
    evidence = "1" * 64
    rule = {
        "rule_id": "bounded-yield-v2",
        "target_type": "text_memory",
        "trigger": "when the answer is C for a bounded estimate",
        "principle": "mass balance bounds the physically possible result",
        "action": "select option C only after checking the limiting amount",
        "validation": "verify the chosen value remains within physical bounds",
        "evidence_count": 1,
        "evidence_digests": [evidence],
        "first_seen_cycle": 1,
        "last_confirmed_cycle": 1,
        "contradiction_count": 0,
    }
    payload = {
        "category": "Yield_Prediction",
        "confirmed_principles": [],
        "provisional_principles": [rule],
        "common_failure_modes": [],
        "option_elimination_checks": [],
        "retired_or_contradicted": [],
        "output_discipline": [],
        "do": [],
        "avoid": [],
        "validate": [],
        "when_applicable": [],
        "retired_or_superseded": [],
    }

    rendered = _render_supervised_structured_memory(
        json.dumps(payload, sort_keys=True, separators=(",", ":"))
    )

    assert _contains_answer_map(rendered) is False
    assert "answer is C" not in rendered
    assert "option C" not in rendered
    assert _supporting_task_set_hash((evidence,)) in rendered
    rule_schema = _SUPERVISED_OUTPUT_SCHEMA["properties"]["provisional_principles"]
    assert "supporting_train_ordinals_hash" not in rule_schema["items"]["properties"]

    rule["supporting_train_ordinals_hash"] = _sha("model-authored-uid")
    with pytest.raises(ReflectorBoundaryError, match="REFLECTOR_LAST_MESSAGE_INVALID"):
        _render_supervised_structured_memory(json.dumps(payload))


def test_core_owns_current_auxiliary_packet_provenance() -> None:
    previous = _sha("previous-packet")
    current = _sha("current-packet")
    visible_supporting_set_hash = _sha("visible-supporting-task-set")
    allowed = frozenset({previous, current})

    assert _bind_auxiliary_source_evidence(
        reported_evidence_digests=(previous,),
        allowed_evidence_digests=allowed,
        current_packet_digest=current,
    ) == (current,)
    assert _bind_auxiliary_source_evidence(
        reported_evidence_digests=(current, visible_supporting_set_hash),
        allowed_evidence_digests=allowed,
        current_packet_digest=current,
    ) == (current,)

    with pytest.raises(
        TaskwiseCoreEvolutionError,
        match="TASKWISE_ARTIFACT_VALIDATION_FAILED",
    ):
        _bind_auxiliary_source_evidence(
            reported_evidence_digests=(_sha("invented-or-private-identifier"),),
            allowed_evidence_digests=allowed,
            current_packet_digest=current,
        )


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
                "openevo-rollout-jsonl:sha256:"
                + _sha(f"synthetic-transcript-{task_index}-{round_index}")
            ),
        ),
        safe_feedback=safe_signal_from_private_evaluation(evaluation),
        dataset_sha256=task.dataset_sha256,
    )
    return trajectory, evaluation


def test_trajectory_rejects_non_openevo_transcript_reference() -> None:
    task = _task(0)
    evaluation = ChemBench4KPrivateEvaluator().evaluate(task=task, raw_completion="A")
    with pytest.raises(ValueError, match="not bound to an OpenEvo Rollout transcript"):
        SupervisedTrajectoryV2.from_attempt(
            task_uid=task.uid,
            task_index=0,
            category=task.category,
            round_index=0,
            session_id="supervised-v2-session-0-0",
            prompt=_prompt(task),
            attempt=RawAttempt(
                response="A",
                transcript_reference=TranscriptReference("synthetic-transcript"),
            ),
            safe_feedback=safe_signal_from_private_evaluation(evaluation),
            dataset_sha256=task.dataset_sha256,
        )


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


def test_primary_memory_outside_train_evidence_fails_before_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        core_methods,
        "_generate_reflector_markdown",
        lambda *_args, **_kwargs: _memory(_sha("invented-packet-evidence")),
    )
    root = tmp_path / "primary-evidence-failure"
    bridge = TaskwiseCoreEvolutionBridgeV1(
        db_path=root / "evolution.sqlite3",
        artifact_root=root / "artifacts",
        executable_registry=_registry(tmp_path / "primary-evidence-registry"),
        checkpoint_path=root / "checkpoints.jsonl",
        memory_limits=SUPERVISED_MEMORY_LIMITS_V2,
    )
    try:
        task = _task(0)
        trajectory, evaluation = _attempt(
            task,
            task_index=0,
            round_index=0,
            response="A",
        )
        packet = SupervisedEvolutionPacketV2.from_evaluations(
            task=task,
            training_task_ordinal=1,
            round_index=0,
            evaluations=(evaluation,),
            trajectory_ids=(trajectory.trajectory_id,),
            predecessor_memory=None,
            predecessor_artifact_id=None,
            predecessor_skill=None,
            predecessor_skill_artifact_id=None,
            predecessor_agent_system=None,
            predecessor_agent_system_artifact_id=None,
        )
        request = TaskwiseCoreUpdateRequestV1(
            task_uid=task.uid,
            task_index=0,
            round_index=0,
            update_index=1,
            trajectories=(trajectory,),
            predecessor=None,
            validator_forbidden_literals=_trajectory_forbidden_literals(trajectory),
            supervised_packet=packet,
        )
        with pytest.raises(
            TaskwiseCoreEvolutionError,
            match="TASKWISE_ARTIFACT_VALIDATION_FAILED",
        ):
            bridge.apply_update(
                request,
                test_only_allow_synthetic_reflector=True,
            )
        with bridge._store.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM contexts").fetchone()[0] == 0
            text_rows = connection.execute(
                "SELECT promoted FROM artifacts WHERE type='text_memory'"
            ).fetchall()
        assert [row["promoted"] for row in text_rows] == [0]
        assert not (root / "checkpoints.jsonl").exists()
        diagnostics = list((root / "private_core_failure_diagnostics").glob("*.json"))
        assert len(diagnostics) == 1
        failure = json.loads(diagnostics[0].read_text(encoding="utf-8"))
        assert failure["stage"] == "ARTIFACT_VALIDATION"
        assert failure["finding_code"] == "TASKWISE_ARTIFACT_VALIDATION_FAILED"
        assert {
            value["finding_code"] for value in failure["validator_finding_evidence"]
        } == {"memory_evidence_outside_train_chain"}
        with pytest.raises(TaskwiseCoreEvolutionError):
            bridge.apply_update(
                request,
                test_only_allow_synthetic_reflector=True,
            )
    finally:
        bridge.close()


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
            text_job_configs = [
                json.loads(row["config_json"])
                for row in connection.execute(
                    "SELECT config_json FROM jobs "
                    "WHERE method = 'text_memory_expel_reflector' ORDER BY rowid"
                ).fetchall()
            ]
            event_rows = connection.execute(
                "SELECT source, payload_path FROM events ORDER BY rowid"
            ).fetchall()
            counts = dict(
                connection.execute(
                    "SELECT type, COUNT(*) FROM artifacts "
                    "WHERE type IN ('text_memory','skill_bundle','agent_system') "
                    "GROUP BY type"
                ).fetchall()
            )
        event_provenance = set()
        for row in event_rows:
            assert row["source"] == "chembench.supervised_transfer.core.v2"
            payload = json.loads(Path(row["payload_path"]).read_text(encoding="utf-8"))
            trajectory_metadata = payload["payload"]["session_result"]["trajectory"][
                "metadata"
            ]
            session_metadata = payload["payload"]["session_result"]["metadata"]
            assert (
                trajectory_metadata["source_execution_provenance_sha256"]
                == session_metadata["source_execution_provenance_sha256"]
            )
            event_provenance.add(
                trajectory_metadata["source_execution_provenance_sha256"]
            )
        assert event_provenance == {
            config["lineage"]["source_execution_provenance_sha256"]
            for config in text_job_configs
        }
        assert counts == {"agent_system": 4, "skill_bundle": 4, "text_memory": 4}
    finally:
        bridge.close()


class _SyntheticBridge:
    def __init__(self, bridge: TaskwiseCoreEvolutionBridgeV1, active: list[str]) -> None:
        self.bridge = bridge
        self.active = active
        self.requests = []

    def apply_update(self, request):
        self.requests.append(request)
        self.active.append(request.supervised_packet.digest)
        return self.bridge.apply_update(
            request,
            test_only_allow_synthetic_reflector=True,
        )

    def issue_runtime_context(self, result):
        return self.bridge.issue_runtime_context(result)


class _OwnedSyntheticBridge(_SyntheticBridge):
    def __init__(
        self,
        bridge: TaskwiseCoreEvolutionBridgeV1,
        active: list[str],
        active_category: list[str],
        category: str,
    ) -> None:
        super().__init__(bridge, active)
        self.active_category = active_category
        self.category = category

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.bridge.close()

    def apply_update(self, request):
        self.active_category.append(self.category)
        return super().apply_update(request)


class _FailingOwnedSyntheticBridge(_OwnedSyntheticBridge):
    def __init__(
        self,
        bridge: TaskwiseCoreEvolutionBridgeV1,
        active: list[str],
        active_category: list[str],
        category: str,
        online_counter: list[int],
        fail_at: int,
        is_online_canary: bool,
    ) -> None:
        super().__init__(bridge, active, active_category, category)
        self.online_counter = online_counter
        self.fail_at = fail_at
        self.is_online_canary = is_online_canary

    def apply_update(self, request):
        if self.is_online_canary:
            self.online_counter[0] += 1
            if self.online_counter[0] == self.fail_at:
                raise ReflectorBoundaryError("REFLECTOR_LAST_MESSAGE_INVALID")
        return super().apply_update(request)


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
            transcript_reference=TranscriptReference(
                "openevo-rollout-jsonl:sha256:" + _sha(request.session_id)
            ),
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


def test_task_retry_window_fails_closed_before_a_new_attempt_after_stall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    experiment = object.__new__(SupervisedTransferExperimentV2)
    experiment.run_id = "stv2-synthetic-stall-0001"
    experiment._state = {"stage": "CONTROL_TRAIN"}
    attempts: list[str] = []
    public_events: list[dict[str, object]] = []
    clock = SimpleNamespace(value=0.0)

    def fail_without_completion(_self, _executor, **_kwargs):
        attempts.append("attempted")
        raise SupervisedTaskExecutionErrorV2(
            SupervisedTaskExecutionCodeV2.TASK_FAILED,
            completion_exists=False,
        )

    def advance_clock(seconds: float) -> None:
        clock.value += seconds

    monkeypatch.setattr(
        SupervisedTransferExperimentV2,
        "_execute_single_session",
        fail_without_completion,
    )
    monkeypatch.setattr(
        SupervisedTransferExperimentV2,
        "_append_public",
        lambda _self, payload: public_events.append(payload),
    )
    monkeypatch.setattr(
        "openevo_chembench.supervised_transfer_v2.experiment._EXECUTOR_STALL_SECONDS",
        60,
    )
    monkeypatch.setattr(
        "openevo_chembench.supervised_transfer_v2.experiment.time.monotonic",
        lambda: clock.value,
    )
    monkeypatch.setattr(
        "openevo_chembench.supervised_transfer_v2.experiment.time.sleep",
        advance_clock,
    )

    with pytest.raises(SupervisedExperimentV2Error) as captured:
        experiment._execute_session(
            object(),  # type: ignore[arg-type]
            task=SimpleNamespace(uid="synthetic-task"),  # type: ignore[arg-type]
            context=None,
            logical_arm="control_train",
            executor_arm="control",
            task_ordinal=0,
            round_index=0,
            stage="CONTROL_TRAIN",
        )

    assert captured.value.finding_code == "EXECUTOR_STALLED"
    assert attempts == ["attempted"]
    assert len(public_events) == 1
    assert public_events[0]["completion_exists"] is False
    assert public_events[0]["stall_budget_seconds"] == 60
    assert public_events[0]["retry_after_seconds"] == 60.0


def test_final_test_retry_window_stalls_before_claiming_a_second_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "state/private"
    state_path.mkdir(parents=True)
    experiment = object.__new__(SupervisedTransferExperimentV2)
    experiment.run_id = "stv2-synthetic-final-stall-0001"
    experiment._state = {"stage": "FREEZE_THREE_TARGETS"}
    experiment.inputs = SimpleNamespace(
        repository_root=tmp_path,
        source_commit=_sha("source"),
        test=(SimpleNamespace(uid=_sha("synthetic-test"), category="Name_Conversion"),),
        config=SimpleNamespace(
            model="gpt-5.5",
            reasoning_effort="medium",
            digest=_sha("config"),
            payload={"roots": {"state": "state"}},
        ),
    )
    attempts: list[str] = []
    public_events: list[dict[str, object]] = []
    clock = SimpleNamespace(value=0.0)

    def fail_without_completion(_self, _executor, **_kwargs):
        attempts.append("attempted")
        raise SupervisedTaskExecutionErrorV2(
            SupervisedTaskExecutionCodeV2.TASK_FAILED,
            completion_exists=False,
        )

    monkeypatch.setattr(
        SupervisedTransferExperimentV2,
        "_set_stage",
        lambda self, stage: self._state.update(stage=stage),
    )
    monkeypatch.setattr(
        SupervisedTransferExperimentV2,
        "_require_source_frozen",
        lambda _self: None,
    )
    monkeypatch.setattr(
        SupervisedTransferExperimentV2,
        "_execute_session",
        fail_without_completion,
    )
    monkeypatch.setattr(
        SupervisedTransferExperimentV2,
        "_append_public",
        lambda _self, payload: public_events.append(payload),
    )
    monkeypatch.setattr(
        "openevo_chembench.supervised_transfer_v2.experiment._EXECUTOR_STALL_SECONDS",
        60,
    )
    monkeypatch.setattr(
        "openevo_chembench.supervised_transfer_v2.experiment.time.monotonic",
        lambda: clock.value,
    )
    monkeypatch.setattr(
        "openevo_chembench.supervised_transfer_v2.experiment.time.sleep",
        lambda seconds: setattr(clock, "value", clock.value + seconds),
    )

    with pytest.raises(SupervisedExperimentV2Error) as captured:
        experiment._run_final_test(
            FrozenThreeTargetSetV2(
                contexts={},
                artifact_set_digest=_sha("artifacts"),
                receipt_sha256=_sha("receipt"),
            ),
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
        )

    assert captured.value.finding_code == "EXECUTOR_STALLED"
    assert attempts == ["attempted"]
    assert len(public_events) == 1
    assert public_events[0]["completion_exists"] is False
    assert public_events[0]["stall_budget_seconds"] == 60
    ledger_rows = [
        json.loads(line)
        for line in (state_path / "final_test_consumption_ledger_v2.jsonl")
        .read_text()
        .splitlines()
    ]
    assert len(ledger_rows) == 1
    assert ledger_rows[0]["completion_exists"] is False


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
    synthetic_bridge = _SyntheticBridge(bridge, active)
    experiment = SupervisedTransferExperimentV2(
        inputs=inputs,
        run_id="stv2-synthetic-controller-0001",
        run_mode="preflight",
        executor_factory=lambda _arm: executor,
        bridge_factory=lambda _root, _category: synthetic_bridge,
    )
    paid_plan = json.loads(
        (
            experiment.result_root
            / "public/planned_paid_execution_receipt_v2.json"
        ).read_text()
    )
    assert paid_plan["calls"] == {
        "candidate_task_calls": 73,
        "candidate_subscription_readiness_calls_minimum": 73,
        "candidate_subscription_readiness_calls_maximum": 146,
        "reflector_calls": 28,
        "answer_and_reflector_model_calls": 101,
        "total_model_calls": 174,
        "maximum_model_calls": 247,
    }
    assert paid_plan["executor_stall_seconds"] == 1200
    assert paid_plan["task_infrastructure_retry_limit"] == 60
    assert paid_plan["task_infrastructure_retry_seconds"] == 60
    assert paid_plan["final_test_infrastructure_retry_limit"] == 60
    assert paid_plan["final_test_infrastructure_retry_seconds"] == 60
    experiment._state["stage"] = "ONLINE_TRAIN"
    experiment._write_state()
    task = next(value for value in inputs.train if value.category == "Name_Conversion")
    try:
        head = experiment._run_one_online_task(
            executor,
            task=task,
            category_ordinal=0,
            bridge=synthetic_bridge,
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
        assert len(synthetic_bridge.requests) == 3
        source_kinds = {
            _decode_validator_literal(literal)[0]
            for request in synthetic_bridge.requests
            for literal in request.validator_forbidden_literals
        }
        assert "strict" not in source_kinds
        assert source_kinds <= {"uid", "question", "option", "completion"}
        assert {"uid", "option", "completion"} <= source_kinds
        with bridge._store.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 9
    finally:
        bridge.close()


def test_full_preflight_offline_lifecycle_closes_at_73_28_84(
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
    inputs = replace(
        loaded,
        config=SupervisedTransferConfigV2(path=config.path, payload=payload),
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
    active_packet = [""]
    active_category = ["Name_Conversion"]
    monkeypatch.setattr(
        core_methods,
        "_generate_reflector_markdown",
        lambda *_args, **_kwargs: _memory(active_packet[-1]).replace(
            "Name_Conversion",
            active_category[-1],
        ),
    )
    registry = _registry(tmp_path / "full-preflight-registry")
    database_paths: list[Path] = []

    def bridge_factory(root: Path, category: str) -> _OwnedSyntheticBridge:
        database = root / "evolution.sqlite3"
        database_paths.append(database)
        return _OwnedSyntheticBridge(
            TaskwiseCoreEvolutionBridgeV1(
                db_path=database,
                artifact_root=root / "artifacts",
                executable_registry=registry,
                checkpoint_path=root / "private-checkpoints.jsonl",
                memory_limits=SUPERVISED_MEMORY_LIMITS_V2,
            ),
            active_packet,
            active_category,
            category,
        )

    executor = _FakeExecutor()
    experiment = SupervisedTransferExperimentV2(
        inputs=inputs,
        run_id="stv2-offline-full-preflight-0001",
        run_mode="preflight",
        executor_factory=lambda _arm: executor,
        bridge_factory=bridge_factory,
    )

    result = experiment.run_preflight()

    assert result["status"] == "PASS"
    assert experiment._state["status"] == "PREFLIGHT_COMPLETED"
    assert experiment._state["task_sessions"] == 73
    assert experiment._state["reflector_calls"] == 28
    assert experiment._state["core_jobs"] == 84
    assert experiment._state["context_resolutions"] == 84
    assert experiment._state["text_memory_artifacts"] == 28
    assert experiment._state["skill_artifacts"] == 28
    assert experiment._state["agent_system_artifacts"] == 28
    assert len(executor.requests) == 73
    assert len(database_paths) == 10

    totals = {"jobs": 0, "contexts": 0}
    artifact_totals: dict[str, int] = {}
    artifact_ids: set[str] = set()
    for database in database_paths:
        with sqlite3.connect(database) as connection:
            totals["jobs"] += connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            totals["contexts"] += connection.execute(
                "SELECT COUNT(*) FROM contexts"
            ).fetchone()[0]
            for artifact_id, artifact_type in connection.execute(
                "SELECT artifact_id, type FROM artifacts "
                "WHERE type IN ('text_memory','skill_bundle','agent_system')"
            ):
                assert artifact_id not in artifact_ids
                artifact_ids.add(artifact_id)
                artifact_totals[artifact_type] = artifact_totals.get(artifact_type, 0) + 1
    assert totals == {"jobs": 84, "contexts": 84}
    assert artifact_totals == {
        "text_memory": 28,
        "skill_bundle": 28,
        "agent_system": 28,
    }

    event_kinds: dict[str, int] = {}
    for line in experiment.public_events.read_text(encoding="utf-8").splitlines():
        kind = json.loads(line)["kind"]
        event_kinds[kind] = event_kinds.get(kind, 0) + 1
    assert event_kinds == {"TASK_MODEL_EXECUTED": 73, "REFLECTOR_SUPERVISED": 28}

    control = [
        request for request in executor.requests if "control-canary" in request.session_id
    ]
    assert len(control) == 36
    assert all(request.resolved_context is None for request in control)
    online = [
        request for request in executor.requests if "online-canary" in request.session_id
    ]
    assert len(online) == 36
    for task_uid in {request.task_uid for request in online}:
        rounds = [request for request in online if request.task_uid == task_uid]
        assert [request.round_index for request in rounds] == [0, 1, 2, 3]
        assert rounds[0].resolved_context is None
        assert all(request.resolved_context is not None for request in rounds[1:])


def test_second_auxiliary_validator_failure_is_exact_and_never_becomes_a_head(
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
        db_path=tmp_path / "failure-core/evolution.sqlite3",
        artifact_root=tmp_path / "failure-core/artifacts",
        executable_registry=_registry(tmp_path / "failure-registry"),
        checkpoint_path=tmp_path / "failure-core/checkpoints.jsonl",
        memory_limits=SUPERVISED_MEMORY_LIMITS_V2,
    )
    task = _task(0)
    trajectory, evaluation = _attempt(task, task_index=0, round_index=0, response="B")
    packet = SupervisedEvolutionPacketV2.from_evaluations(
        task=task,
        training_task_ordinal=1,
        round_index=0,
        evaluations=(evaluation,),
        trajectory_ids=(trajectory.trajectory_id,),
        predecessor_memory=None,
        predecessor_artifact_id=None,
        predecessor_skill=None,
        predecessor_skill_artifact_id=None,
        predecessor_agent_system=None,
        predecessor_agent_system_artifact_id=None,
    )
    active_packet.append(packet.digest)
    request = TaskwiseCoreUpdateRequestV1(
        task_uid=task.uid,
        task_index=0,
        round_index=0,
        update_index=1,
        trajectories=(trajectory,),
        predecessor=None,
        validator_forbidden_literals=_trajectory_forbidden_literals(trajectory),
        supervised_packet=packet,
    )
    original_inspector = supervised_core.inspect_supervised_auxiliary_artifact_v2

    def reject_agent_system(payload, *, target_id, **kwargs):
        inspection = original_inspector(payload, target_id=target_id, **kwargs)
        if target_id == "agent_system":
            return inspection.model_copy(
                update={"finding_codes": ("auxiliary_structure_invalid",)}
            )
        return inspection

    monkeypatch.setattr(
        supervised_core,
        "inspect_supervised_auxiliary_artifact_v2",
        reject_agent_system,
    )
    try:
        with pytest.raises(
            TaskwiseCoreEvolutionError,
            match="TASKWISE_ARTIFACT_VALIDATION_FAILED",
        ):
            bridge.apply_update(
                request,
                test_only_allow_synthetic_reflector=True,
            )
        assert bridge._head is None
        assert bridge._terminal_finding == "TASKWISE_CORE_UPDATE_FAILED"
        assert not (tmp_path / "failure-core/checkpoints.jsonl").exists()
        with bridge._store.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3
            rows = connection.execute(
                "SELECT type, promoted FROM artifacts "
                "WHERE type IN ('text_memory','skill_bundle','agent_system') "
                "ORDER BY type"
            ).fetchall()
        assert len(rows) == 3
        assert {row[0] for row in rows} == {
            "text_memory",
            "skill_bundle",
            "agent_system",
        }
        assert any(row[1] == 0 for row in rows)
        with pytest.raises(TaskwiseCoreEvolutionError, match="TASKWISE_STREAM_TERMINAL"):
            bridge.apply_update(
                request,
                test_only_allow_synthetic_reflector=True,
            )
    finally:
        bridge.close()


def test_reflector_contract_canary_prioritizes_two_risky_categories_and_is_not_reusable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = Path(__file__).resolve().parents[4]
    config = load_config_v2(
        (
            repository
            / "benchmarks/chembench/configs/supervised_transfer_v2/"
            "chembench_supervised_transfer_v2.yaml"
        ).resolve()
    )
    payload = dict(config.payload)
    payload["roots"] = {
        "results": str(tmp_path / "results"),
        "state": str(tmp_path / "state"),
        "reports": str(tmp_path / "reports"),
    }
    loaded = load_experiment_inputs_v2(repository, config, require_reflector_runtime=False)
    inputs = replace(
        loaded,
        config=SupervisedTransferConfigV2(path=config.path, payload=payload),
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
    active_packet = [""]
    active_category = ["Name_Conversion"]
    monkeypatch.setattr(
        core_methods,
        "_generate_reflector_markdown",
        lambda *_args, **_kwargs: _memory(active_packet[-1]).replace(
            "Name_Conversion", active_category[-1]
        ),
    )
    registry = _registry(tmp_path / "contract-canary-registry")

    def bridge_factory(root: Path, category: str) -> _OwnedSyntheticBridge:
        return _OwnedSyntheticBridge(
            TaskwiseCoreEvolutionBridgeV1(
                db_path=root / "evolution.sqlite3",
                artifact_root=root / "artifacts",
                executable_registry=registry,
                checkpoint_path=root / "private-checkpoints.jsonl",
                memory_limits=SUPERVISED_MEMORY_LIMITS_V2,
            ),
            active_packet,
            active_category,
            category,
        )

    executor = _FakeExecutor()
    experiment = SupervisedTransferExperimentV2(
        inputs=inputs,
        run_id="stv2-offline-contract-canary-0001",
        run_mode="contract_canary",
        readiness_receipt_sha256=_sha("readiness"),
        executor_factory=lambda _arm: executor,
        bridge_factory=bridge_factory,
    )
    authority = experiment.run_contract_canary()

    assert authority["status"] == "PASS"
    assert authority["categories"] == ["Temperature_Prediction", "Name_Conversion"]
    assert authority["task_sessions"] == 8
    assert authority["reflector_calls"] == 6
    assert authority["core_jobs"] == 18
    assert authority["context_resolutions"] == 18
    assert authority["text_memory_artifacts"] == 6
    assert authority["skill_artifacts"] == 6
    assert authority["agent_system_artifacts"] == 6
    assert authority["artifacts_reusable"] is False
    assert [request.round_index for request in executor.requests] == [
        0,
        1,
        2,
        3,
        0,
        1,
        2,
        3,
    ]

    preflight = SupervisedTransferExperimentV2(
        inputs=inputs,
        run_id="stv2-offline-preflight-authority-0001",
        run_mode="preflight",
        contract_canary_run_id="stv2-offline-contract-canary-0001",
        readiness_receipt_sha256=_sha("readiness"),
        executor_factory=lambda _arm: _FakeExecutor(),
        bridge_factory=bridge_factory,
    )
    preflight._verify_contract_canary_authority()
    assert preflight._state["contract_canary_authority_sha256"] == hashlib.sha256(
        (
            experiment.result_root
            / "public/contract_canary_authority_v2.json"
        ).read_bytes()
    ).hexdigest()


@pytest.mark.parametrize(
    ("failure_stage", "expected_code"),
    (
        ("core_job", "TASKWISE_CORE_JOB_FAILED"),
        ("promotion", "TASKWISE_ARTIFACT_PROMOTION_FAILED"),
        ("context", "TASKWISE_CONTEXT_RESOLUTION_FAILED"),
    ),
)
def test_auxiliary_lifecycle_failures_are_exact_terminal_and_never_carried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
    expected_code: str,
) -> None:
    active_packet = [""]
    monkeypatch.setattr(
        core_methods,
        "_generate_reflector_markdown",
        lambda *_args, **_kwargs: _memory(active_packet[-1]),
    )
    root = tmp_path / failure_stage
    bridge = TaskwiseCoreEvolutionBridgeV1(
        db_path=root / "evolution.sqlite3",
        artifact_root=root / "artifacts",
        executable_registry=_registry(tmp_path / f"{failure_stage}-registry"),
        checkpoint_path=root / "checkpoints.jsonl",
        memory_limits=SUPERVISED_MEMORY_LIMITS_V2,
    )
    task = _task(0)
    trajectory, evaluation = _attempt(task, task_index=0, round_index=0, response="B")
    packet = SupervisedEvolutionPacketV2.from_evaluations(
        task=task,
        training_task_ordinal=1,
        round_index=0,
        evaluations=(evaluation,),
        trajectory_ids=(trajectory.trajectory_id,),
        predecessor_memory=None,
        predecessor_artifact_id=None,
        predecessor_skill=None,
        predecessor_skill_artifact_id=None,
        predecessor_agent_system=None,
        predecessor_agent_system_artifact_id=None,
    )
    active_packet.append(packet.digest)
    request = TaskwiseCoreUpdateRequestV1(
        task_uid=task.uid,
        task_index=0,
        round_index=0,
        update_index=1,
        trajectories=(trajectory,),
        predecessor=None,
        validator_forbidden_literals=_trajectory_forbidden_literals(trajectory),
        supervised_packet=packet,
    )
    calls = {"value": 0}
    if failure_stage == "core_job":
        original_run_once = supervised_core.run_once

        def fail_second_job(*args, **kwargs):
            calls["value"] += 1
            if calls["value"] == 2:
                return False
            return original_run_once(*args, **kwargs)

        monkeypatch.setattr(supervised_core, "run_once", fail_second_job)
    elif failure_stage == "promotion":
        original_promotion = bridge._store.update_artifact_promotion

        def fail_second_promotion(*args, **kwargs):
            calls["value"] += 1
            if calls["value"] == 2:
                return SimpleNamespace(promoted=False)
            return original_promotion(*args, **kwargs)

        monkeypatch.setattr(bridge._store, "update_artifact_promotion", fail_second_promotion)
    else:
        original_resolution = bridge._store.resolve_materialized_context

        def fail_second_resolution(*args, **kwargs):
            calls["value"] += 1
            if calls["value"] == 2:
                raise RuntimeError("synthetic context failure")
            return original_resolution(*args, **kwargs)

        monkeypatch.setattr(
            bridge._store,
            "resolve_materialized_context",
            fail_second_resolution,
        )
    try:
        with pytest.raises(TaskwiseCoreEvolutionError) as captured:
            bridge.apply_update(request, test_only_allow_synthetic_reflector=True)
        assert captured.value.finding_code == expected_code
        assert bridge._head is None
        assert bridge._terminal_finding == "TASKWISE_CORE_UPDATE_FAILED"
        assert not (root / "checkpoints.jsonl").exists()
        with pytest.raises(TaskwiseCoreEvolutionError, match="TASKWISE_STREAM_TERMINAL"):
            bridge.apply_update(request, test_only_allow_synthetic_reflector=True)
    finally:
        bridge.close()


@pytest.mark.parametrize("fail_at", (1, 15, 27))
def test_preflight_reflector_failure_at_early_middle_or_final_cycle_is_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_at: int,
) -> None:
    repository = Path(__file__).resolve().parents[4]
    config = load_config_v2(
        (
            repository
            / "benchmarks/chembench/configs/supervised_transfer_v2/"
            "chembench_supervised_transfer_v2.yaml"
        ).resolve()
    )
    payload = dict(config.payload)
    payload["roots"] = {
        "results": str(tmp_path / "results"),
        "state": str(tmp_path / "state"),
        "reports": str(tmp_path / "reports"),
    }
    loaded = load_experiment_inputs_v2(repository, config, require_reflector_runtime=False)
    inputs = replace(
        loaded,
        config=SupervisedTransferConfigV2(path=config.path, payload=payload),
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
    active_packet = [""]
    active_category = ["Name_Conversion"]
    online_counter = [0]
    monkeypatch.setattr(
        core_methods,
        "_generate_reflector_markdown",
        lambda *_args, **_kwargs: _memory(active_packet[-1]).replace(
            "Name_Conversion", active_category[-1]
        ),
    )
    registry = _registry(tmp_path / f"failure-position-{fail_at}-registry")

    def bridge_factory(root: Path, category: str) -> _FailingOwnedSyntheticBridge:
        return _FailingOwnedSyntheticBridge(
            TaskwiseCoreEvolutionBridgeV1(
                db_path=root / "evolution.sqlite3",
                artifact_root=root / "artifacts",
                executable_registry=registry,
                checkpoint_path=root / "private-checkpoints.jsonl",
                memory_limits=SUPERVISED_MEMORY_LIMITS_V2,
            ),
            active_packet,
            active_category,
            category,
            online_counter,
            fail_at,
            "online_canary" in root.parts,
        )

    run_id = f"stv2-offline-failure-position-{fail_at:02d}"
    experiment = SupervisedTransferExperimentV2(
        inputs=inputs,
        run_id=run_id,
        run_mode="preflight",
        executor_factory=lambda _arm: _FakeExecutor(),
        bridge_factory=bridge_factory,
    )
    with pytest.raises(ReflectorBoundaryError, match="REFLECTOR_LAST_MESSAGE_INVALID"):
        experiment.run_preflight()
    assert online_counter == [fail_at]
    assert experiment._state["status"] == "FAIL_CLOSED"
    assert experiment._state["failure_code"] == "REFLECTOR_LAST_MESSAGE_INVALID"
    assert not (
        experiment.result_root / "public/preflight_authority_v2.json"
    ).exists()
    with pytest.raises(SupervisedExperimentV2Error, match="RUN_ID_ALREADY_EXISTS"):
        SupervisedTransferExperimentV2(
            inputs=inputs,
            run_id=run_id,
            run_mode="preflight",
            executor_factory=lambda _arm: _FakeExecutor(),
            bridge_factory=bridge_factory,
        )


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
