"""Fail-closed Train, Probe, and frozen Test orchestration for the protocol."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from collections import defaultdict
from collections.abc import Iterable
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from openevo_chembench.chembench4k_dataset import ChemBench4KDatasetLoader
from openevo_chembench.chembench4k_evaluation import (
    ChemBench4KPrivateEvaluator,
    PrivateChemBench4KEvaluation,
)
from openevo_chembench.chembench4k_models import (
    CHEMBENCH4K_CATEGORIES,
    PrivateChemBench4KTask,
    RenderedChemBench4KPrompt,
)
from openevo_chembench.chembench4k_prompt import render_official_five_shot_prompt
from openevo_chembench.frozen_runtime_v2 import CoreResolvedTextMemoryV2
from openevo_chembench.local_codex_executor import LocalCodexCLIExecutor
from openevo_chembench.models import RawAttempt
from openevo_chembench.supervised_transfer_v1.common import (
    canonical_json_bytes,
    canonical_pretty_json_bytes,
    sha256_bytes,
    write_private_file,
    write_public_file,
)
from openevo_chembench.supervised_transfer_v1.config import (
    PROTOCOL_ID,
    ROUNDS_PER_TRAIN_TASK,
    TOTAL_MODEL_CALLS,
    TRAIN_CHECKPOINTS,
    SupervisedTransferConfigV1,
)
from openevo_chembench.supervised_transfer_v1.packet import (
    SupervisedEvolutionPacketV1,
)
from openevo_chembench.supervised_transfer_v1.split_v2 import (
    load_private_partition_v2,
)
from openevo_chembench.taskwise_config_v1 import load_taskwise_config_v1
from openevo_chembench.taskwise_core_evolution_v1 import (
    METHOD_ID,
    TaskwiseCoreEvolutionBridgeV1,
    TaskwiseCoreUpdateRequestV1,
    TaskwiseCoreUpdateResultV1,
    _trajectory_forbidden_literals,
    build_supervised_core_bridge_at_roots_v1,
)
from openevo_chembench.taskwise_feedback_v1 import safe_signal_from_private_evaluation
from openevo_chembench.taskwise_online_runner_v1 import TaskwiseAgentRequestV1
from openevo_chembench.taskwise_trajectory_v1 import TaskwiseTrajectoryV1

DRY_RUN_SCHEMA = "chembench_supervised_transfer_dry_run_v1"
RUN_STATE_SCHEMA = "chembench_supervised_transfer_run_state_v1"
FROZEN_RECEIPT_SCHEMA = "FrozenTransferReceiptV1"
TEST_USE_RECEIPT_SCHEMA = "FrozenTestManifestUseReceiptV1"
SPLIT_ISOLATION_RECEIPT = "split_isolation_receipt_v2.json"
FORMAL_TASK_CALLS = TOTAL_MODEL_CALLS - 900
FORMAL_REFLECTOR_CALLS = 900
SMOKE_CANARY_TASK_CALLS = 73
SMOKE_CANARY_REFLECTOR_CALLS = 19
_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}\Z", re.ASCII)


class SupervisedExperimentError(RuntimeError):
    """Content-free terminal or infrastructure failure."""

    def __init__(self, finding_code: str) -> None:
        if re.fullmatch(r"[A-Z][A-Z0-9_]{2,95}", finding_code) is None:
            raise ValueError("experiment finding code is invalid")
        self.finding_code = finding_code
        super().__init__(finding_code)


@dataclass(frozen=True, slots=True)
class ExperimentInputsV1:
    repository_root: Path
    config: SupervisedTransferConfigV1
    loader: ChemBench4KDatasetLoader
    train: tuple[PrivateChemBench4KTask, ...]
    probe: tuple[PrivateChemBench4KTask, ...]
    test: tuple[PrivateChemBench4KTask, ...]
    manifest_root: Path
    split_receipt_sha256: str
    source_commit: str
    codex_cli_version: str


@dataclass(frozen=True, slots=True)
class SessionOutcomeV1:
    evaluation: PrivateChemBench4KEvaluation
    attempt: RawAttempt
    session_id: str
    context_binding: dict[str, object]


@dataclass(frozen=True, slots=True)
class FrozenMemorySetV1:
    checkpoint: int
    memories: dict[str, CoreResolvedTextMemoryV2 | None]
    artifact_ids: dict[str, str | None]
    payload_sha256: dict[str, str | None]
    context_sha256: dict[str, str | None]


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def load_experiment_inputs_v1(
    repository_root: Path,
    config: SupervisedTransferConfigV1,
) -> ExperimentInputsV1:
    repository = repository_root.resolve(strict=True)
    snapshot = (repository / config.dataset_root).resolve(strict=True)
    loader = ChemBench4KDatasetLoader(snapshot_root=snapshot)
    manifests = repository / "benchmarks/chembench/manifests/supervised_transfer_v1"
    split_receipt = manifests / SPLIT_ISOLATION_RECEIPT
    if not split_receipt.is_file():
        raise SupervisedExperimentError("SPLIT_ISOLATION_RECEIPT_MISSING")
    train = load_private_partition_v2(
        loader,
        partition="train",
        private_manifest=manifests / "train_private_manifest.jsonl",
    )
    probe = load_private_partition_v2(
        loader,
        partition="probe",
        private_manifest=manifests / "probe_private_manifest.jsonl",
    )
    test = load_private_partition_v2(
        loader,
        partition="test_primary",
        private_manifest=manifests / "test_primary_private_manifest.jsonl",
    )
    _require_partition_isolation(train, probe, test)
    source_commit = _git_output(repository, ("rev-parse", "HEAD"))
    if _git_output(repository, ("diff", "--", "src/openevo")):
        raise SupervisedExperimentError("SRC_OPENEVO_NOT_PRISTINE")
    codex_version = _codex_version(repository)
    return ExperimentInputsV1(
        repository_root=repository,
        config=config,
        loader=loader,
        train=train,
        probe=probe,
        test=test,
        manifest_root=manifests,
        split_receipt_sha256=sha256_bytes(split_receipt.read_bytes()),
        source_commit=source_commit,
        codex_cli_version=codex_version,
    )


def build_complete_dry_run_v1(inputs: ExperimentInputsV1) -> dict[str, object]:
    """Render every public prompt and simulate all immutable stage transitions."""

    prompts: dict[str, str] = {}
    demonstration_uids: dict[str, tuple[str, ...]] = {}
    for task in (*inputs.train, *inputs.probe, *inputs.test):
        rendered = _render_prompt(inputs.loader, task)
        prompts[task.uid] = sha256_bytes(rendered.text.encode("utf-8"))
        demonstration_uids[task.category] = rendered.demonstration_uids
    if len(prompts) != 990 or any(len(values) != 5 for values in demonstration_uids.values()):
        raise SupervisedExperimentError("DRY_RUN_PROMPT_CARDINALITY_INVALID")

    train_order = _balanced_training_order(inputs.train)
    checkpoints = {
        checkpoint: {
            category: sum(
                task.category == category
                for task in train_order[: checkpoint * len(CHEMBENCH4K_CATEGORIES)]
            )
            for category in CHEMBENCH4K_CATEGORIES
        }
        for checkpoint in TRAIN_CHECKPOINTS
    }
    if any(set(counts.values()) != {checkpoint} for checkpoint, counts in checkpoints.items()):
        raise SupervisedExperimentError("DRY_RUN_CHECKPOINT_SCHEDULE_INVALID")
    call_plan = {
        "control_train_task": len(inputs.train) * 3,
        "online_train_task": len(inputs.train) * 3,
        "online_train_reflector": len(inputs.train) * 2,
        "probe_task": len(TRAIN_CHECKPOINTS) * len(inputs.probe) * 2,
        "test_task": len(inputs.test) * 2,
    }
    if sum(call_plan.values()) != TOTAL_MODEL_CALLS:
        raise SupervisedExperimentError("DRY_RUN_CALL_BUDGET_INVALID")
    return {
        "schema_version": DRY_RUN_SCHEMA,
        "protocol_id": PROTOCOL_ID,
        "status": "PASS",
        "model_calls": 0,
        "planned_model_calls": TOTAL_MODEL_CALLS,
        "planned_task_calls": FORMAL_TASK_CALLS,
        "planned_reflector_calls": FORMAL_REFLECTOR_CALLS,
        "smoke_canary_task_calls": SMOKE_CANARY_TASK_CALLS,
        "smoke_canary_reflector_calls": SMOKE_CANARY_REFLECTOR_CALLS,
        "call_plan": call_plan,
        "train_order_sha256": _uid_order_sha256(train_order),
        "probe_order_sha256": _uid_order_sha256(inputs.probe),
        "test_order_sha256": _uid_order_sha256(inputs.test),
        "prompt_binding_sha256": sha256_bytes(canonical_json_bytes(prompts)),
        "demonstration_binding_sha256": sha256_bytes(
            canonical_json_bytes(
                {category: list(values) for category, values in demonstration_uids.items()}
            )
        ),
        "checkpoints": checkpoints,
        "split_receipt_sha256": inputs.split_receipt_sha256,
        "source_commit": inputs.source_commit,
        "codex_cli_version": inputs.codex_cli_version,
        "reflector_input_scope": "Train only",
        "probe_evolution_jobs": 0,
        "test_evolution_jobs": 0,
        "test_manifest": "test_primary",
        "ready_for_paid_smoke": True,
    }


class SupervisedTransferExperimentV1:
    """Single-use foreground experiment; failures preserve all run evidence."""

    def __init__(self, *, inputs: ExperimentInputsV1, run_id: str) -> None:
        if type(inputs) is not ExperimentInputsV1:
            raise TypeError("inputs must be exact ExperimentInputsV1")
        if type(run_id) is not str or _RUN_ID_RE.fullmatch(run_id) is None:
            raise ValueError("run_id is invalid")
        self.inputs = inputs
        self.run_id = run_id
        self.result_root = inputs.repository_root / inputs.config.result_root / "runs" / run_id
        self.state_root = inputs.repository_root / inputs.config.state_root / "runs" / run_id
        if self.result_root.exists() or self.state_root.exists():
            raise SupervisedExperimentError("RUN_ID_ALREADY_EXISTS")
        self.result_root.mkdir(parents=True, mode=0o755)
        self.state_root.mkdir(parents=True, mode=0o700)
        self.state_root.chmod(0o700)
        (self.result_root / "public").mkdir(mode=0o755)
        (self.state_root / "private").mkdir(mode=0o700)
        self.public_events = self.result_root / "public/events.jsonl"
        self.private_events = self.state_root / "private/events.jsonl"
        _create_file(self.public_events, 0o644)
        _create_file(self.private_events, 0o600)
        self._state: dict[str, object] = {
            "schema_version": RUN_STATE_SCHEMA,
            "protocol_id": PROTOCOL_ID,
            "run_id": run_id,
            "status": "INITIALIZED",
            "stage": "INITIALIZED",
            "source_commit": inputs.source_commit,
            "split_receipt_sha256": inputs.split_receipt_sha256,
            "test_manifest": "test_primary",
            "task_sessions": 0,
            "reflector_completions": 0,
            "core_jobs": 0,
            "core_artifacts": 0,
            "security_findings": 0,
            "context_findings": 0,
            "artifact_findings": 0,
            "infrastructure_failures": 0,
            "created_at_utc": utc_now(),
            "last_progress_utc": utc_now(),
        }
        self._write_state()
        self._evaluator = ChemBench4KPrivateEvaluator()
        self._bridges: dict[str, TaskwiseCoreEvolutionBridgeV1] = {}
        self._heads: dict[str, TaskwiseCoreUpdateResultV1 | None] = {
            category: None for category in CHEMBENCH4K_CATEGORIES
        }
        self._frozen_checkpoints: dict[int, FrozenMemorySetV1] = {}

    def run(self) -> dict[str, object]:
        try:
            with ExitStack() as stack:
                control, online = self._open_executors(stack)
                self._run_update_smoke(online)
                self._run_online_canary(online)
                self._run_control_canary(control)
                self._run_probe_smoke(control, online)
                self._open_formal_bridges(stack)
                checkpoint_zero = self._freeze_checkpoint(0)
                self._run_probe_checkpoint(0, checkpoint_zero, control, online)
                self._run_control_train(control)
                self._run_online_train_with_probes(control, online)
                frozen = self._freeze_final_memory()
                self._run_final_test(frozen, control, online)
            self._set_stage("REPORTING")
            from openevo_chembench.supervised_transfer_v1.reporting import (
                build_all_reports_v1,
            )

            report = build_all_reports_v1(
                repository_root=self.inputs.repository_root,
                run_result_root=self.result_root,
                run_state_root=self.state_root,
                run_id=self.run_id,
            )
            self._state["status"] = "COMPLETED"
            self._state["stage"] = "COMPLETED"
            self._state["completed_at_utc"] = utc_now()
            self._state["report_sha256"] = report["final_report_sha256"]
            self._write_state()
            return report
        except BaseException as exc:
            self._state["status"] = "FAIL_CLOSED"
            self._state["failure_code"] = (
                exc.finding_code
                if type(exc) is SupervisedExperimentError
                else _closed_exception_code(exc)
            )
            self._state["failed_at_utc"] = utc_now()
            self._write_state()
            raise

    def _open_executors(
        self,
        stack: ExitStack,
    ) -> tuple[LocalCodexCLIExecutor, LocalCodexCLIExecutor]:
        root = self.inputs.repository_root
        control_config = replace(
            load_taskwise_config_v1(
                root / "benchmarks/chembench/configs/control_canary9_taskwise_online_v1.yaml"
            ),
            codex_cli_version=self.inputs.codex_cli_version.removeprefix("codex-cli "),
        )
        online_config = replace(
            load_taskwise_config_v1(
                root / "benchmarks/chembench/configs/online_canary9_taskwise_online_v1.yaml"
            ),
            codex_cli_version=self.inputs.codex_cli_version.removeprefix("codex-cli "),
        )
        control = stack.enter_context(
            LocalCodexCLIExecutor(
                config=control_config,
                task_timeout_seconds=self.inputs.config.executor.timeout_seconds,
                diagnostic_root=self.state_root / "private/executor/control",
            )
        )
        online = stack.enter_context(
            LocalCodexCLIExecutor(
                config=online_config,
                task_timeout_seconds=self.inputs.config.executor.timeout_seconds,
                diagnostic_root=self.state_root / "private/executor/online",
            )
        )
        return control, online

    def _open_formal_bridges(self, stack: ExitStack) -> None:
        self._set_stage("ONLINE_CORE_INITIALIZATION")
        framework_lock = (self.inputs.repository_root / self.inputs.config.framework_lock).resolve()
        for category in CHEMBENCH4K_CATEGORIES:
            bridge = build_supervised_core_bridge_at_roots_v1(
                state_root=(self.state_root / "private/core" / category).resolve(),
                framework_lock=framework_lock,
                timeout_seconds=self.inputs.config.reflector_timeout_seconds,
            )
            self._bridges[category] = stack.enter_context(bridge)

    def _run_update_smoke(self, executor: LocalCodexCLIExecutor) -> None:
        self._set_stage("SUPERVISED_UPDATE_SMOKE")
        task = _balanced_training_order(self.inputs.train)[0]
        with ExitStack() as stack:
            bridge = stack.enter_context(self._new_ephemeral_bridge("update_smoke", task.category))
            outcome = self._execute_session(
                executor,
                task=task,
                memory=None,
                logical_arm="online_smoke",
                executor_arm="online",
                task_ordinal=0,
                round_index=0,
                stage="SUPERVISED_UPDATE_SMOKE",
            )
            trajectory = self._trajectory(task, 0, 0, outcome)
            packet = self._packet(task, 0, 0, outcome, None)
            result = bridge.apply_update(
                TaskwiseCoreUpdateRequestV1(
                    task_uid=task.uid,
                    task_index=0,
                    round_index=0,
                    update_index=1,
                    trajectories=(trajectory,),
                    predecessor=None,
                    validator_forbidden_literals=_trajectory_forbidden_literals(trajectory),
                    supervised_packet=packet,
                )
            )
            bridge.issue_runtime_memory(result)
            self._record_core_result("SUPERVISED_UPDATE_SMOKE", task.category, result)

    def _run_online_canary(self, executor: LocalCodexCLIExecutor) -> None:
        self._set_stage("ONLINE_CANARY")
        first_by_category = _first_by_category(self.inputs.train)
        for category in CHEMBENCH4K_CATEGORIES:
            task = first_by_category[category]
            with ExitStack() as stack:
                bridge = stack.enter_context(self._new_ephemeral_bridge("online_canary", category))
                self._run_one_online_task(
                    executor,
                    task=task,
                    category_ordinal=0,
                    bridge=bridge,
                    stage="ONLINE_CANARY",
                    persist_formal_head=False,
                )

    def _run_control_canary(self, executor: LocalCodexCLIExecutor) -> None:
        self._set_stage("CONTROL_CANARY")
        for ordinal, task in enumerate(_first_by_category(self.inputs.train).values()):
            for round_index in range(3):
                self._execute_session(
                    executor,
                    task=task,
                    memory=None,
                    logical_arm="control_canary",
                    executor_arm="control",
                    task_ordinal=ordinal,
                    round_index=round_index,
                    stage="CONTROL_CANARY",
                )

    def _run_probe_smoke(
        self,
        control: LocalCodexCLIExecutor,
        online: LocalCodexCLIExecutor,
    ) -> None:
        del online
        self._set_stage("PROBE_SMOKE")
        first_by_category = _first_by_category(self.inputs.probe)
        for ordinal, task in enumerate(first_by_category.values()):
            for logical_arm in ("control_probe_smoke", "online_probe_smoke"):
                self._execute_session(
                    control,
                    task=task,
                    memory=None,
                    logical_arm=logical_arm,
                    executor_arm="control",
                    task_ordinal=ordinal,
                    round_index=0,
                    stage="PROBE_SMOKE",
                )

    def _run_control_train(self, executor: LocalCodexCLIExecutor) -> None:
        self._set_stage("CONTROL_TRAIN")
        for ordinal, task in enumerate(_balanced_training_order(self.inputs.train)):
            for round_index in range(ROUNDS_PER_TRAIN_TASK):
                self._execute_session(
                    executor,
                    task=task,
                    memory=None,
                    logical_arm="control_train",
                    executor_arm="control",
                    task_ordinal=ordinal,
                    round_index=round_index,
                    stage="CONTROL_TRAIN",
                )

    def _run_online_train_with_probes(
        self,
        control: LocalCodexCLIExecutor,
        online: LocalCodexCLIExecutor,
    ) -> None:
        ordered = _balanced_training_order(self.inputs.train)
        category_ordinals = defaultdict(int)
        for global_ordinal, task in enumerate(ordered):
            self._set_stage("ONLINE_TRAIN")
            local = category_ordinals[task.category]
            self._run_one_online_task(
                online,
                task=task,
                category_ordinal=local,
                bridge=self._bridges[task.category],
                stage="ONLINE_TRAIN",
                persist_formal_head=True,
            )
            category_ordinals[task.category] += 1
            if (global_ordinal + 1) % (10 * len(CHEMBENCH4K_CATEGORIES)) == 0:
                checkpoint = (global_ordinal + 1) // len(CHEMBENCH4K_CATEGORIES)
                frozen = self._freeze_checkpoint(checkpoint)
                self._run_probe_checkpoint(checkpoint, frozen, control, online)
        if set(category_ordinals.values()) != {50}:
            raise SupervisedExperimentError("ONLINE_TRAIN_CATEGORY_COUNT_INVALID")

    def _run_one_online_task(
        self,
        executor: LocalCodexCLIExecutor,
        *,
        task: PrivateChemBench4KTask,
        category_ordinal: int,
        bridge: TaskwiseCoreEvolutionBridgeV1,
        stage: str,
        persist_formal_head: bool,
    ) -> CoreResolvedTextMemoryV2:
        head = bridge.current_head()
        memory0 = None if head is None else bridge.issue_runtime_memory(head)
        round0 = self._execute_session(
            executor,
            task=task,
            memory=memory0,
            logical_arm="online_train" if persist_formal_head else "online_canary",
            executor_arm="online",
            task_ordinal=category_ordinal,
            round_index=0,
            stage=stage,
        )
        trajectory0 = self._trajectory(task, category_ordinal, 0, round0)
        packet0 = self._packet(task, category_ordinal, 0, round0, memory0)
        update1 = bridge.apply_update(
            TaskwiseCoreUpdateRequestV1(
                task_uid=task.uid,
                task_index=category_ordinal,
                round_index=0,
                update_index=1,
                trajectories=(trajectory0,),
                predecessor=None if head is None else head.predecessor_identity(),
                validator_forbidden_literals=_trajectory_forbidden_literals(trajectory0),
                supervised_packet=packet0,
            )
        )
        memory1 = bridge.issue_runtime_memory(update1)
        self._record_core_result(stage, task.category, update1)

        round1 = self._execute_session(
            executor,
            task=task,
            memory=memory1,
            logical_arm="online_train" if persist_formal_head else "online_canary",
            executor_arm="online",
            task_ordinal=category_ordinal,
            round_index=1,
            stage=stage,
        )
        trajectory1 = self._trajectory(task, category_ordinal, 1, round1)
        packet1 = self._packet(task, category_ordinal, 1, round1, memory1)
        update2 = bridge.apply_update(
            TaskwiseCoreUpdateRequestV1(
                task_uid=task.uid,
                task_index=category_ordinal,
                round_index=1,
                update_index=2,
                trajectories=(trajectory0, trajectory1),
                predecessor=update1.predecessor_identity(),
                validator_forbidden_literals=(
                    *_trajectory_forbidden_literals(trajectory0),
                    *_trajectory_forbidden_literals(trajectory1),
                ),
                supervised_packet=packet1,
            )
        )
        memory2 = bridge.issue_runtime_memory(update2)
        self._record_core_result(stage, task.category, update2)
        self._execute_session(
            executor,
            task=task,
            memory=memory2,
            logical_arm="online_train" if persist_formal_head else "online_canary",
            executor_arm="online",
            task_ordinal=category_ordinal,
            round_index=2,
            stage=stage,
        )
        if persist_formal_head:
            self._heads[task.category] = update2
        return memory2

    def _run_probe_checkpoint(
        self,
        checkpoint: int,
        frozen: FrozenMemorySetV1,
        control: LocalCodexCLIExecutor,
        online: LocalCodexCLIExecutor,
    ) -> None:
        self._set_stage(f"PROBE_CHECKPOINT_{checkpoint:02d}")
        jobs_before = self._formal_core_job_count()
        for ordinal, task in enumerate(self.inputs.probe):
            self._execute_session(
                control,
                task=task,
                memory=None,
                logical_arm="control_probe",
                executor_arm="control",
                task_ordinal=ordinal,
                round_index=0,
                stage=f"PROBE_CHECKPOINT_{checkpoint:02d}",
                checkpoint=checkpoint,
            )
            memory = frozen.memories[task.category]
            executor = control if memory is None else online
            self._execute_session(
                executor,
                task=task,
                memory=memory,
                logical_arm="online_probe",
                executor_arm="control" if memory is None else "online",
                task_ordinal=ordinal + 1 if memory is not None else ordinal,
                round_index=0,
                stage=f"PROBE_CHECKPOINT_{checkpoint:02d}",
                checkpoint=checkpoint,
            )
        if self._formal_core_job_count() != jobs_before:
            raise SupervisedExperimentError("PROBE_CREATED_EVOLUTION_JOB")

    def _freeze_checkpoint(self, checkpoint: int) -> FrozenMemorySetV1:
        existing = self._frozen_checkpoints.get(checkpoint)
        if existing is not None:
            return existing
        memories: dict[str, CoreResolvedTextMemoryV2 | None] = {}
        artifacts: dict[str, str | None] = {}
        payloads: dict[str, str | None] = {}
        contexts: dict[str, str | None] = {}
        for category in CHEMBENCH4K_CATEGORIES:
            head = self._heads[category]
            memory = None if head is None else self._bridges[category].issue_runtime_memory(head)
            memories[category] = memory
            artifacts[category] = None if memory is None else memory.core_artifact_id
            payloads[category] = None if memory is None else memory.artifact_payload_sha256
            contexts[category] = None if memory is None else memory.context_resolution_digest
            checkpoint_path = (
                self.state_root
                / "private/checkpoint_memory"
                / category
                / f"checkpoint_{checkpoint:02d}.md"
            )
            write_private_file(
                checkpoint_path,
                (
                    b"# Empty generation-zero category memory\n"
                    if memory is None
                    else memory.markdown.encode("utf-8")
                ),
                replace=False,
            )
        receipt = {
            "schema_version": "CategoryMemoryCheckpointReceiptV1",
            "protocol_id": PROTOCOL_ID,
            "checkpoint": checkpoint,
            "artifact_ids": artifacts,
            "artifact_payload_sha256": payloads,
            "context_resolution_sha256": contexts,
            "train_items_per_category": checkpoint,
            "probe_feedback_used": False,
            "created_at_utc": utc_now(),
        }
        write_public_file(
            self.result_root / "public/checkpoints" / f"checkpoint_{checkpoint:02d}.json",
            canonical_pretty_json_bytes(receipt),
        )
        frozen = FrozenMemorySetV1(
            checkpoint=checkpoint,
            memories=memories,
            artifact_ids=artifacts,
            payload_sha256=payloads,
            context_sha256=contexts,
        )
        self._frozen_checkpoints[checkpoint] = frozen
        return frozen

    def _freeze_final_memory(self) -> FrozenMemorySetV1:
        self._set_stage("FREEZE_FINAL_MEMORY")
        frozen = self._freeze_checkpoint(50)
        if any(value is None for value in frozen.artifact_ids.values()):
            raise SupervisedExperimentError("FINAL_MEMORY_SET_INCOMPLETE")
        receipt = {
            "schema_version": FROZEN_RECEIPT_SCHEMA,
            "protocol_id": PROTOCOL_ID,
            "source_commit": self.inputs.source_commit,
            "split_receipt_sha256": self.inputs.split_receipt_sha256,
            "test_manifest": "test_primary",
            "test_manifest_sha256": sha256_bytes(
                (self.inputs.manifest_root / "test_primary_private_manifest.jsonl").read_bytes()
            ),
            "artifact_ids": frozen.artifact_ids,
            "artifact_payload_sha256": frozen.payload_sha256,
            "context_resolution_sha256": frozen.context_sha256,
            "memory_provenance": "Train-only SupervisedEvolutionPacketV1",
            "probe_feedback_used": False,
            "test_feedback_used": False,
            "core_job_count": self._formal_core_job_count(),
            "frozen_at_utc": utc_now(),
        }
        path = self.result_root / "public/frozen_transfer_receipt_v1.json"
        write_public_file(path, canonical_pretty_json_bytes(receipt))
        self._state["frozen_transfer_receipt_sha256"] = sha256_bytes(path.read_bytes())
        from openevo_chembench.supervised_transfer_v1.desktop_export import (
            export_desktop_audit_v1,
        )

        self._state["post_train_desktop_package"] = export_desktop_audit_v1(
            repository_root=self.inputs.repository_root,
            run_result_root=self.result_root,
            run_state_root=self.state_root,
            run_id=self.run_id,
            phase="post_train",
        )
        self._write_state()
        return frozen

    def _run_final_test(
        self,
        frozen: FrozenMemorySetV1,
        control: LocalCodexCLIExecutor,
        online: LocalCodexCLIExecutor,
    ) -> None:
        self._set_stage("FINAL_TEST")
        jobs_before = self._formal_core_job_count()
        use_receipt = self.result_root / "public/test_primary_use_receipt.json"
        write_public_file(
            use_receipt,
            canonical_pretty_json_bytes(
                {
                    "schema_version": TEST_USE_RECEIPT_SCHEMA,
                    "protocol_id": PROTOCOL_ID,
                    "manifest": "test_primary",
                    "manifest_sha256": sha256_bytes(
                        (
                            self.inputs.manifest_root
                            / "test_primary_private_manifest.jsonl"
                        ).read_bytes()
                    ),
                    "reason": "PRIMARY_PREREGISTERED_TEST",
                    "maximum_uses": 1,
                    "started_at_utc": utc_now(),
                }
            ),
        )
        for ordinal, task in enumerate(self.inputs.test):
            self._execute_session(
                control,
                task=task,
                memory=None,
                logical_arm="control_test",
                executor_arm="control",
                task_ordinal=ordinal,
                round_index=0,
                stage="FINAL_TEST",
            )
            self._execute_session(
                online,
                task=task,
                memory=frozen.memories[task.category],
                logical_arm="online_test",
                executor_arm="online",
                task_ordinal=ordinal + 1,
                round_index=0,
                stage="FINAL_TEST",
            )
        if self._formal_core_job_count() != jobs_before:
            raise SupervisedExperimentError("TEST_CREATED_EVOLUTION_JOB")

    def _execute_session(
        self,
        executor: LocalCodexCLIExecutor,
        *,
        task: PrivateChemBench4KTask,
        memory: CoreResolvedTextMemoryV2 | None,
        logical_arm: str,
        executor_arm: Literal["control", "online"],
        task_ordinal: int,
        round_index: Literal[0, 1, 2],
        stage: str,
        checkpoint: int | None = None,
    ) -> SessionOutcomeV1:
        session_id = self._session_id(stage, logical_arm, task, round_index)
        prompt = _render_prompt(self.inputs.loader, task)
        attempt_event = {
            "schema_version": "supervised_task_attempt_v1",
            "kind": "TASK_MODEL_ATTEMPTED",
            "stage": stage,
            "logical_arm": logical_arm,
            "task_uid": task.uid,
            "category": task.category,
            "task_ordinal": task_ordinal,
            "round_index": round_index,
            "checkpoint": checkpoint,
            "session_id": session_id,
            "prompt_sha256": sha256_bytes(prompt.text.encode("utf-8")),
            "started_at_utc": utc_now(),
        }
        self._append_private(attempt_event)
        try:
            attempt = executor.execute_taskwise(
                TaskwiseAgentRequestV1(
                    rendered_public_prompt=prompt.text,
                    resolved_text_memory=memory,
                    session_id=session_id,
                    arm=executor_arm,
                    task_ordinal=task_ordinal,
                    round_index=round_index,
                    run_id=self.run_id,
                    task_uid=task.uid,
                )
            )
            context = executor.consume_taskwise_context_receipt(session_id)
            context.require_match()
            evaluation = self._evaluator.evaluate(task=task, raw_completion=attempt.response)
        except BaseException:
            self._state["infrastructure_failures"] = (
                int(self._state["infrastructure_failures"]) + 1
            )
            self._write_state()
            raise
        public = {
            "schema_version": "supervised_transfer_public_completion_v1",
            "kind": "TASK_MODEL_EXECUTED",
            "stage": stage,
            "logical_arm": logical_arm,
            "task_uid": task.uid,
            "category": task.category,
            "task_ordinal": task_ordinal,
            "round_index": round_index,
            "checkpoint": checkpoint,
            "session_id": session_id,
            "official_prediction": evaluation.official.prediction,
            "official_parse_status": evaluation.official.status.value,
            "strict_prediction": evaluation.strict.prediction,
            "strict_parse_status": evaluation.strict.status.value,
            "memory_artifact_id": None if memory is None else memory.core_artifact_id,
            "context_binding_sha256": context.digest,
            "completed_at_utc": utc_now(),
        }
        private = {
            **public,
            "schema_version": "supervised_transfer_private_evaluation_v1",
            "kind": "PRIVATE_EVALUATED",
            "target": task.target,
            "raw_completion": evaluation.raw_completion,
            "correct": evaluation.correct,
            "transcript_reference": attempt.transcript_reference.reference,
        }
        self._append_public(public)
        self._append_private(private)
        self._state["task_sessions"] = int(self._state["task_sessions"]) + 1
        self._state["last_progress_utc"] = utc_now()
        self._state["stage"] = stage
        self._state["arm"] = logical_arm
        self._state["category"] = task.category
        self._state["round_or_update"] = round_index
        self._write_state()
        return SessionOutcomeV1(
            evaluation=evaluation,
            attempt=attempt,
            session_id=session_id,
            context_binding=context.to_public_dict(),
        )

    def _trajectory(
        self,
        task: PrivateChemBench4KTask,
        category_ordinal: int,
        round_index: Literal[0, 1],
        outcome: SessionOutcomeV1,
    ) -> TaskwiseTrajectoryV1:
        return TaskwiseTrajectoryV1.from_attempt(
            task_uid=task.uid,
            task_index=category_ordinal,
            category=task.category,
            round_index=round_index,
            session_id=outcome.session_id,
            prompt=_render_prompt(self.inputs.loader, task),
            attempt=outcome.attempt,
            safe_feedback=safe_signal_from_private_evaluation(outcome.evaluation),
            dataset_sha256=task.dataset_sha256,
        )

    def _packet(
        self,
        task: PrivateChemBench4KTask,
        category_ordinal: int,
        round_index: Literal[0, 1],
        outcome: SessionOutcomeV1,
        predecessor: CoreResolvedTextMemoryV2 | None,
    ) -> SupervisedEvolutionPacketV1:
        return SupervisedEvolutionPacketV1.from_evaluation(
            task=task,
            training_task_ordinal=category_ordinal + 1,
            round_index=round_index,
            evaluation=outcome.evaluation,
            predecessor_memory=None if predecessor is None else predecessor.markdown,
            predecessor_artifact_id=(
                None if predecessor is None else predecessor.core_artifact_id
            ),
            session_id=outcome.session_id,
        )

    def _record_core_result(
        self,
        stage: str,
        category: str,
        result: TaskwiseCoreUpdateResultV1,
    ) -> None:
        self._append_public(
            {
                "schema_version": "supervised_transfer_core_update_public_v1",
                "kind": "REFLECTOR_SUPERVISED",
                "stage": stage,
                "category": category,
                "task_uid": result.task_uid,
                "task_index": result.task_index,
                "update_index": result.update_index,
                "global_update_ordinal": result.global_update_ordinal,
                "packet_sha256": result.supervised_packet_sha256,
                "input_schema_sha256": result.supervised_input_schema_sha256,
                "reflector_prompt_sha256": result.supervised_reflector_prompt_sha256,
                "method_id": METHOD_ID,
                "method_descriptor_sha256": result.method_descriptor_digest,
                "job_id": result.job_id,
                "artifact_id": result.core_artifact_id,
                "artifact_payload_sha256": result.artifact_payload_sha256,
                "context_resolution_sha256": result.context_resolution_digest,
                "validation_receipt_sha256": result.validation_receipt_sha256,
                "memory_metrics": result.memory_inspection.to_public_payload(),
                "completed_at_utc": utc_now(),
            }
        )
        self._state["reflector_completions"] = (
            int(self._state["reflector_completions"]) + 1
        )
        self._state["core_jobs"] = int(self._state["core_jobs"]) + 1
        self._state["core_artifacts"] = int(self._state["core_artifacts"]) + 1
        self._state["round_or_update"] = f"update_{result.update_index}"
        self._state["last_progress_utc"] = utc_now()
        self._write_state()

    def _new_ephemeral_bridge(
        self,
        scope: str,
        category: str,
    ) -> TaskwiseCoreEvolutionBridgeV1:
        return build_supervised_core_bridge_at_roots_v1(
            state_root=(self.state_root / "private/core_preflight" / scope / category).resolve(),
            framework_lock=(
                self.inputs.repository_root / self.inputs.config.framework_lock
            ).resolve(),
            timeout_seconds=self.inputs.config.reflector_timeout_seconds,
        )

    def _formal_core_job_count(self) -> int:
        total = 0
        for bridge in self._bridges.values():
            with bridge._store.connect() as connection:
                total += int(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
        return total

    def _session_id(
        self,
        stage: str,
        arm: str,
        task: PrivateChemBench4KTask,
        round_index: int,
    ) -> str:
        ordinal = int(self._state["task_sessions"]) + 1
        identity = hashlib.sha256(
            f"{self.run_id}:{stage}:{arm}:{task.uid}:{round_index}:{ordinal}".encode()
        ).hexdigest()[:20]
        return f"st-{ordinal:06d}-{round_index}-{identity}"

    def _set_stage(self, stage: str) -> None:
        self._state["stage"] = stage
        self._state["last_progress_utc"] = utc_now()
        self._write_state()
        print(
            json.dumps(
                {
                    "stage": stage,
                    "task_sessions": self._state["task_sessions"],
                    "reflector_completions": self._state["reflector_completions"],
                    "last_progress": self._state["last_progress_utc"],
                },
                sort_keys=True,
            ),
            flush=True,
        )

    def _append_public(self, payload: dict[str, object]) -> None:
        _append_jsonl(self.public_events, payload, mode=0o644)

    def _append_private(self, payload: dict[str, object]) -> None:
        _append_jsonl(self.private_events, payload, mode=0o600)

    def _write_state(self) -> None:
        write_public_file(
            self.result_root / "public/run_state.json",
            canonical_pretty_json_bytes(self._state),
        )


def _render_prompt(
    loader: ChemBench4KDatasetLoader,
    task: PrivateChemBench4KTask,
) -> RenderedChemBench4KPrompt:
    return render_official_five_shot_prompt(
        task.to_public(),
        category_dev=loader.load_category(task.category, split="dev"),
    )


def _balanced_training_order(
    tasks: tuple[PrivateChemBench4KTask, ...],
) -> tuple[PrivateChemBench4KTask, ...]:
    grouped = {
        category: tuple(task for task in tasks if task.category == category)
        for category in CHEMBENCH4K_CATEGORIES
    }
    if any(len(values) != 50 for values in grouped.values()):
        raise SupervisedExperimentError("TRAIN_CATEGORY_CARDINALITY_INVALID")
    return tuple(
        grouped[category][ordinal]
        for ordinal in range(50)
        for category in CHEMBENCH4K_CATEGORIES
    )


def _first_by_category(
    tasks: tuple[PrivateChemBench4KTask, ...],
) -> dict[str, PrivateChemBench4KTask]:
    result: dict[str, PrivateChemBench4KTask] = {}
    for task in tasks:
        result.setdefault(task.category, task)
    if tuple(result) != CHEMBENCH4K_CATEGORIES:
        raise SupervisedExperimentError("CATEGORY_ORDER_INVALID")
    return result


def _require_partition_isolation(
    train: tuple[PrivateChemBench4KTask, ...],
    probe: tuple[PrivateChemBench4KTask, ...],
    test: tuple[PrivateChemBench4KTask, ...],
) -> None:
    sets = tuple({task.uid for task in tasks} for tasks in (train, probe, test))
    if sets[0] & sets[1] or sets[0] & sets[2] or sets[1] & sets[2]:
        raise SupervisedExperimentError("TRAIN_PROBE_TEST_OVERLAP")


def _uid_order_sha256(tasks: Iterable[PrivateChemBench4KTask]) -> str:
    return sha256_bytes(("\n".join(task.uid for task in tasks) + "\n").encode("ascii"))


def _codex_version(repository: Path) -> str:
    completed = subprocess.run(
        ("codex", "--version"),
        cwd=repository,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    value = completed.stdout.strip()
    if completed.returncode != 0 or re.fullmatch(r"codex-cli [0-9]+\.[0-9]+\.[0-9]+", value) is None:
        raise SupervisedExperimentError("CODEX_VERSION_UNAVAILABLE")
    return value


def _git_output(repository: Path, arguments: tuple[str, ...]) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=repository,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise SupervisedExperimentError("GIT_IDENTITY_UNAVAILABLE")
    return completed.stdout.strip()


def _create_file(path: Path, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    os.close(descriptor)


def _append_jsonl(path: Path, payload: dict[str, object], *, mode: int) -> None:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != mode:
        raise SupervisedExperimentError("EVENT_FILE_UNSAFE")
    encoded = canonical_json_bytes(payload)
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND)
    with os.fdopen(descriptor, "ab", buffering=0) as stream:
        stream.write(encoded)
        os.fsync(stream.fileno())


def _closed_exception_code(exc: BaseException) -> str:
    if isinstance(exc, OSError):
        return "OS_ERROR"
    if isinstance(exc, TimeoutError):
        return "TIMEOUT"
    if isinstance(exc, ValueError):
        return "VALUE_ERROR"
    if isinstance(exc, TypeError):
        return "TYPE_ERROR"
    return "UNCLASSIFIED_EXPERIMENT_FAILURE"


__all__ = [
    "DRY_RUN_SCHEMA",
    "ExperimentInputsV1",
    "FrozenMemorySetV1",
    "SupervisedExperimentError",
    "SupervisedTransferExperimentV1",
    "build_complete_dry_run_v1",
    "load_experiment_inputs_v1",
]
