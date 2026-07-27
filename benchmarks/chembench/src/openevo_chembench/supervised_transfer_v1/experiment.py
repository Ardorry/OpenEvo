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
from openevo_chembench.local_codex_executor import (
    LocalCodexCLIExecutor,
    LocalCodexExecutionError,
)
from openevo_chembench.models import RawAttempt
from openevo_chembench.supervised_transfer_v1.artifacts import (
    CoreResolvedSupervisedContextV1,
)
from openevo_chembench.supervised_transfer_v1.common import (
    canonical_json_bytes,
    canonical_pretty_json_bytes,
    sha256_bytes,
    write_private_file,
    write_public_file,
)
from openevo_chembench.supervised_transfer_v1.config import (
    FORMAL_CORE_ARTIFACTS,
    FORMAL_CORE_JOBS,
    PREFLIGHT_CORE_ARTIFACTS,
    PREFLIGHT_CORE_JOBS,
    PROTOCOL_ID,
    ROUNDS_PER_TRAIN_TASK,
    TOTAL_MODEL_CALLS,
    TRAIN_CHECKPOINTS,
    SupervisedTransferConfigV1,
)
from openevo_chembench.supervised_transfer_v1.context_binding import (
    SupervisedAgentRequestV1,
)
from openevo_chembench.supervised_transfer_v1.packet import (
    SupervisedEvolutionPacketV1,
)
from openevo_chembench.supervised_transfer_v1.preflight import (
    verify_preflight_authority_v1,
    write_preflight_authority_v1,
)
from openevo_chembench.supervised_transfer_v1.split_v2 import (
    load_private_partition_v2,
)
from openevo_chembench.supervised_transfer_v1.test_ledger import (
    claim_test_manifest_use_v1,
    complete_test_manifest_use_v1,
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
from openevo_chembench.taskwise_trajectory_v1 import TaskwiseTrajectoryV1

DRY_RUN_SCHEMA = "chembench_supervised_transfer_dry_run_v1"
RUN_STATE_SCHEMA = "chembench_supervised_transfer_run_state_v1"
FROZEN_RECEIPT_SCHEMA = "FrozenTransferReceiptV2"
TEST_USE_RECEIPT_SCHEMA = "FrozenTestManifestUseReceiptV1"
SPLIT_ISOLATION_RECEIPT = "split_isolation_receipt_v2.json"
FORMAL_TASK_CALLS = TOTAL_MODEL_CALLS - 900 - (90 * 2)
FORMAL_REFLECTOR_CALLS = 900
SMOKE_CANARY_TASK_CALLS = 73
SMOKE_CANARY_REFLECTOR_CALLS = 19
_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}\Z", re.ASCII)
_TEST_MANIFESTS = ("test_primary", "test_recovery_01")
_TEST_INFRASTRUCTURE_ATTEMPTS = 3

_STAGE_TRANSITIONS: dict[str, frozenset[str]] = {
    "INITIALIZED": frozenset({"SUPERVISED_UPDATE_SMOKE", "CONTROL_TRAIN"}),
    "SUPERVISED_UPDATE_SMOKE": frozenset({"ONLINE_CANARY"}),
    "ONLINE_CANARY": frozenset({"CONTROL_CANARY"}),
    "CONTROL_CANARY": frozenset({"PROBE_SMOKE"}),
    "PROBE_SMOKE": frozenset({"PROBE_CHECKPOINT_00"}),
    "CONTROL_TRAIN": frozenset({"ONLINE_CORE_INITIALIZATION"}),
    "ONLINE_CORE_INITIALIZATION": frozenset({"ONLINE_TRAIN"}),
    "ONLINE_TRAIN": frozenset(
        {
            "ONLINE_TRAIN",
            "PROBE_CHECKPOINT_10",
            "PROBE_CHECKPOINT_20",
            "PROBE_CHECKPOINT_30",
            "PROBE_CHECKPOINT_40",
            "PROBE_CHECKPOINT_50",
        }
    ),
    "PROBE_CHECKPOINT_10": frozenset({"ONLINE_TRAIN"}),
    "PROBE_CHECKPOINT_20": frozenset({"ONLINE_TRAIN"}),
    "PROBE_CHECKPOINT_30": frozenset({"ONLINE_TRAIN"}),
    "PROBE_CHECKPOINT_40": frozenset({"ONLINE_TRAIN"}),
    "PROBE_CHECKPOINT_50": frozenset({"FREEZE_FINAL_CONTEXT"}),
    "FREEZE_FINAL_CONTEXT": frozenset({"FINAL_TEST"}),
    "FINAL_TEST": frozenset({"REPORTING"}),
    "REPORTING": frozenset({"COMPLETED"}),
}


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
    test_manifest: str


@dataclass(frozen=True, slots=True)
class SessionOutcomeV1:
    evaluation: PrivateChemBench4KEvaluation
    attempt: RawAttempt
    session_id: str
    context_binding: dict[str, object]


@dataclass(frozen=True, slots=True)
class FrozenContextSetV1:
    checkpoint: int
    contexts: dict[str, CoreResolvedSupervisedContextV1 | None]
    artifact_ids: dict[str, dict[str, str | None]]
    payload_sha256: dict[str, dict[str, str | None]]
    context_sha256: dict[str, dict[str, str | None]]


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def load_experiment_inputs_v1(
    repository_root: Path,
    config: SupervisedTransferConfigV1,
    *,
    test_manifest: str = "test_primary",
) -> ExperimentInputsV1:
    if test_manifest not in _TEST_MANIFESTS:
        raise SupervisedExperimentError("TEST_MANIFEST_NOT_PREREGISTERED")
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
        partition=test_manifest,
        private_manifest=manifests / f"{test_manifest}_private_manifest.jsonl",
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
        test_manifest=test_manifest,
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
        "planned_core_jobs": FORMAL_CORE_JOBS,
        "planned_core_artifacts": FORMAL_CORE_ARTIFACTS,
        "smoke_canary_task_calls": SMOKE_CANARY_TASK_CALLS,
        "smoke_canary_reflector_calls": SMOKE_CANARY_REFLECTOR_CALLS,
        "preflight_task_calls": 253,
        "preflight_reflector_calls": 19,
        "preflight_core_jobs": PREFLIGHT_CORE_JOBS,
        "preflight_core_artifacts": PREFLIGHT_CORE_ARTIFACTS,
        "formal_task_calls": FORMAL_TASK_CALLS,
        "formal_reflector_calls": FORMAL_REFLECTOR_CALLS,
        "formal_core_jobs": FORMAL_CORE_JOBS,
        "formal_core_artifacts": FORMAL_CORE_ARTIFACTS,
        "evolution_targets": ["text_memory", "skill_bundle", "agent_system"],
        "preflight_stage_order": [
            "SUPERVISED_UPDATE_SMOKE",
            "ONLINE_CANARY",
            "CONTROL_CANARY",
            "PROBE_SMOKE",
            "PROBE_CHECKPOINT_00",
            "PREFLIGHT_COMPLETED",
        ],
        "formal_stage_order": [
            "CONTROL_TRAIN",
            "ONLINE_CORE_INITIALIZATION",
            "ONLINE_TRAIN",
            "PROBE_CHECKPOINT_10_20_30_40_50",
            "FREEZE_FINAL_CONTEXT",
            "FINAL_TEST",
            "REPORTING",
            "COMPLETED",
        ],
        "formal_first_paid_session": {
            "stage": "CONTROL_TRAIN",
            "logical_arm": "control_train",
            "task_ordinal": 0,
            "round_index": 0,
        },
        "formal_checkpoint_zero_source": "verified aggregate-only preflight authority",
        "formal_imported_train_rows": 0,
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
        "test_manifest": inputs.test_manifest,
        "ready_for_paid_smoke": True,
    }


class SupervisedTransferExperimentV1:
    """Single-use preflight or formal experiment with closed stage transitions."""

    def __init__(
        self,
        *,
        inputs: ExperimentInputsV1,
        run_id: str,
        run_mode: Literal["preflight", "formal"],
        preflight_run_id: str | None = None,
    ) -> None:
        if type(inputs) is not ExperimentInputsV1:
            raise TypeError("inputs must be exact ExperimentInputsV1")
        if type(run_id) is not str or _RUN_ID_RE.fullmatch(run_id) is None:
            raise ValueError("run_id is invalid")
        if run_mode not in {"preflight", "formal"}:
            raise ValueError("run_mode is invalid")
        if run_mode == "preflight" and preflight_run_id is not None:
            raise ValueError("preflight runs cannot consume a preflight authority")
        if run_mode == "formal" and (
            type(preflight_run_id) is not str
            or _RUN_ID_RE.fullmatch(preflight_run_id) is None
            or preflight_run_id == run_id
        ):
            raise ValueError("formal runs require a distinct preflight run ID")
        self.inputs = inputs
        self.run_id = run_id
        self.run_mode = run_mode
        self.preflight_run_id = preflight_run_id
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
            "run_mode": run_mode,
            "status": "INITIALIZED",
            "stage": "INITIALIZED",
            "source_commit": inputs.source_commit,
            "split_receipt_sha256": inputs.split_receipt_sha256,
            "test_manifest": inputs.test_manifest,
            "preflight_run_id": preflight_run_id,
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
        self._frozen_checkpoints: dict[int, FrozenContextSetV1] = {}

    def run_preflight(self) -> dict[str, object]:
        if self.run_mode != "preflight":
            raise SupervisedExperimentError("RUN_MODE_MISMATCH")
        try:
            with ExitStack() as stack:
                control, online = self._open_executors(stack)
                self._run_update_smoke(online)
                self._run_online_canary(online)
                self._run_control_canary(control)
                self._run_probe_smoke(control, online)
                checkpoint_zero = self._freeze_checkpoint(0)
                self._run_probe_checkpoint(0, checkpoint_zero, control, online)
            authority, authority_sha256 = write_preflight_authority_v1(
                inputs=self.inputs,
                source_run_id=self.run_id,
                issued_at_utc=utc_now(),
            )
            self._state["preflight_authority_sha256"] = authority_sha256
            self._state["status"] = "PREFLIGHT_COMPLETED"
            self._state["stage"] = "PREFLIGHT_COMPLETED"
            self._state["completed_at_utc"] = utc_now()
            self._write_state()
            return {
                "status": "PREFLIGHT_COMPLETED",
                "run_id": self.run_id,
                "preflight_authority_sha256": authority_sha256,
                "task_sessions": authority["task_sessions"],
                "reflector_completions": authority["reflector_completions"],
                "test_model_calls": 0,
            }
        except BaseException as exc:
            self._fail_closed(exc)
            raise

    def run_formal(self) -> dict[str, object]:
        if self.run_mode != "formal" or self.preflight_run_id is None:
            raise SupervisedExperimentError("RUN_MODE_MISMATCH")
        try:
            authority, authority_sha256 = verify_preflight_authority_v1(
                inputs=self.inputs,
                source_run_id=self.preflight_run_id,
            )
            self._state["preflight_authority_sha256"] = authority_sha256
            self._state["preflight_checkpoint_zero_aggregate_rows"] = authority[
                "checkpoint_zero_aggregate_rows"
            ]
            self._write_state()
            with ExitStack() as stack:
                control, online = self._open_executors(stack)
                self._run_control_train(control)
                self._open_formal_bridges(stack)
                self._freeze_checkpoint(0)
                self._run_online_train_with_probes(control, online)
                frozen = self._freeze_final_context()
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
                preflight_authority=authority,
            )
            self._state["status"] = "COMPLETED"
            self._set_stage("COMPLETED")
            self._state["completed_at_utc"] = utc_now()
            self._state["report_sha256"] = report["final_report_sha256"]
            self._write_state()
            return report
        except BaseException as exc:
            self._fail_closed(exc)
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
                context=None,
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
            bridge.issue_runtime_context(result)
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
                    context=None,
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
                    context=None,
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
                    context=None,
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
    ) -> CoreResolvedSupervisedContextV1:
        head = bridge.current_head()
        context0 = None if head is None else bridge.issue_runtime_context(head)
        round0 = self._execute_session(
            executor,
            task=task,
            context=context0,
            logical_arm="online_train" if persist_formal_head else "online_canary",
            executor_arm="online",
            task_ordinal=category_ordinal,
            round_index=0,
            stage=stage,
        )
        trajectory0 = self._trajectory(task, category_ordinal, 0, round0)
        packet0 = self._packet(task, category_ordinal, 0, round0, context0)
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
        context1 = bridge.issue_runtime_context(update1)
        self._record_core_result(stage, task.category, update1)

        round1 = self._execute_session(
            executor,
            task=task,
            context=context1,
            logical_arm="online_train" if persist_formal_head else "online_canary",
            executor_arm="online",
            task_ordinal=category_ordinal,
            round_index=1,
            stage=stage,
        )
        trajectory1 = self._trajectory(task, category_ordinal, 1, round1)
        packet1 = self._packet(task, category_ordinal, 1, round1, context1)
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
        context2 = bridge.issue_runtime_context(update2)
        self._record_core_result(stage, task.category, update2)
        self._execute_session(
            executor,
            task=task,
            context=context2,
            logical_arm="online_train" if persist_formal_head else "online_canary",
            executor_arm="online",
            task_ordinal=category_ordinal,
            round_index=2,
            stage=stage,
        )
        if persist_formal_head:
            self._heads[task.category] = update2
        return context2

    def _run_probe_checkpoint(
        self,
        checkpoint: int,
        frozen: FrozenContextSetV1,
        control: LocalCodexCLIExecutor,
        online: LocalCodexCLIExecutor,
    ) -> None:
        self._set_stage(f"PROBE_CHECKPOINT_{checkpoint:02d}")
        jobs_before = self._formal_core_job_count()
        for ordinal, task in enumerate(self.inputs.probe):
            self._execute_session(
                control,
                task=task,
                context=None,
                logical_arm="control_probe",
                executor_arm="control",
                task_ordinal=ordinal,
                round_index=0,
                stage=f"PROBE_CHECKPOINT_{checkpoint:02d}",
                checkpoint=checkpoint,
            )
            context = frozen.contexts[task.category]
            executor = control if context is None else online
            self._execute_session(
                executor,
                task=task,
                context=context,
                logical_arm="online_probe",
                executor_arm="control" if context is None else "online",
                task_ordinal=ordinal,
                round_index=0,
                stage=f"PROBE_CHECKPOINT_{checkpoint:02d}",
                checkpoint=checkpoint,
            )
        if self._formal_core_job_count() != jobs_before:
            raise SupervisedExperimentError("PROBE_CREATED_EVOLUTION_JOB")

    def _freeze_checkpoint(self, checkpoint: int) -> FrozenContextSetV1:
        existing = self._frozen_checkpoints.get(checkpoint)
        if existing is not None:
            return existing
        runtime_contexts: dict[str, CoreResolvedSupervisedContextV1 | None] = {}
        artifacts: dict[str, dict[str, str | None]] = {}
        payloads: dict[str, dict[str, str | None]] = {}
        context_digests: dict[str, dict[str, str | None]] = {}
        for category in CHEMBENCH4K_CATEGORIES:
            head = self._heads[category]
            context = (
                None
                if head is None
                else self._bridges[category].issue_runtime_context(head)
            )
            runtime_contexts[category] = context
            target_values = {
                "text_memory": None if context is None else context.memory,
                "skill_bundle": None if context is None else context.skill,
                "agent_system": None if context is None else context.agent_system,
            }
            artifacts[category] = {
                target: None if value is None else value.core_artifact_id
                for target, value in target_values.items()
            }
            payloads[category] = {
                target: None if value is None else value.artifact_payload_sha256
                for target, value in target_values.items()
            }
            context_digests[category] = {
                target: None if value is None else value.context_resolution_digest
                for target, value in target_values.items()
            }
            for target, value in target_values.items():
                checkpoint_path = (
                    self.state_root
                    / "private/checkpoint_context"
                    / target
                    / category
                    / f"checkpoint_{checkpoint:02d}.md"
                )
                markdown = (
                    f"# Empty generation-zero category {target}\n"
                    if value is None
                    else value.markdown
                )
                write_private_file(
                    checkpoint_path,
                    markdown.encode("utf-8"),
                    replace=False,
                )
                if target == "text_memory":
                    write_private_file(
                        self.state_root
                        / "private/checkpoint_memory"
                        / category
                        / f"checkpoint_{checkpoint:02d}.md",
                        markdown.encode("utf-8"),
                        replace=False,
                    )
        receipt = {
            "schema_version": "CategoryContextCheckpointReceiptV1",
            "protocol_id": PROTOCOL_ID,
            "checkpoint": checkpoint,
            "target_order": ["text_memory", "skill_bundle", "agent_system"],
            "artifact_ids": artifacts,
            "artifact_payload_sha256": payloads,
            "context_resolution_sha256": context_digests,
            "train_items_per_category": checkpoint,
            "probe_feedback_used": False,
            "created_at_utc": utc_now(),
        }
        write_public_file(
            self.result_root / "public/checkpoints" / f"checkpoint_{checkpoint:02d}.json",
            canonical_pretty_json_bytes(receipt),
        )
        frozen = FrozenContextSetV1(
            checkpoint=checkpoint,
            contexts=runtime_contexts,
            artifact_ids=artifacts,
            payload_sha256=payloads,
            context_sha256=context_digests,
        )
        self._frozen_checkpoints[checkpoint] = frozen
        return frozen

    def _freeze_final_context(self) -> FrozenContextSetV1:
        self._set_stage("FREEZE_FINAL_CONTEXT")
        self._require_source_still_frozen()
        frozen = self._freeze_checkpoint(50)
        if any(
            value is None
            for category in frozen.artifact_ids.values()
            for value in category.values()
        ):
            raise SupervisedExperimentError("FINAL_CONTEXT_SET_INCOMPLETE")
        receipt = {
            "schema_version": FROZEN_RECEIPT_SCHEMA,
            "protocol_id": PROTOCOL_ID,
            "source_commit": self.inputs.source_commit,
            "split_receipt_sha256": self.inputs.split_receipt_sha256,
            "test_manifest": self.inputs.test_manifest,
            "test_manifest_sha256": sha256_bytes(
                (
                    self.inputs.manifest_root
                    / f"{self.inputs.test_manifest}_private_manifest.jsonl"
                ).read_bytes()
            ),
            "artifact_ids": frozen.artifact_ids,
            "artifact_payload_sha256": frozen.payload_sha256,
            "context_resolution_sha256": frozen.context_sha256,
            "context_targets": ["text_memory", "skill_bundle", "agent_system"],
            "context_provenance": "Train-only SupervisedEvolutionPacketV1",
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

    def _require_source_still_frozen(self) -> None:
        repository = self.inputs.repository_root
        if _git_output(repository, ("rev-parse", "HEAD")) != self.inputs.source_commit:
            raise SupervisedExperimentError("SOURCE_CHANGED_DURING_FORMAL_RUN")
        if _git_output(repository, ("diff", "--", "src/openevo")):
            raise SupervisedExperimentError("SRC_OPENEVO_NOT_PRISTINE")
        if _git_output(
            repository,
            (
                "status",
                "--short",
                "--untracked-files=all",
                "--",
                "benchmarks/chembench",
            ),
        ):
            raise SupervisedExperimentError("BENCHMARK_SOURCE_CHANGED_DURING_FORMAL_RUN")

    def _run_final_test(
        self,
        frozen: FrozenContextSetV1,
        control: LocalCodexCLIExecutor,
        online: LocalCodexCLIExecutor,
    ) -> None:
        self._set_stage("FINAL_TEST")
        jobs_before = self._formal_core_job_count()
        frozen_receipt_sha256 = self._state.get("frozen_transfer_receipt_sha256")
        if type(frozen_receipt_sha256) is not str:
            raise SupervisedExperimentError("FROZEN_TRANSFER_RECEIPT_MISSING")
        claim = claim_test_manifest_use_v1(
            inputs=self.inputs,
            run_id=self.run_id,
            frozen_receipt_sha256=frozen_receipt_sha256,
            claimed_at_utc=utc_now(),
        )
        claim_sha256 = str(claim["entry_sha256"])
        use_receipt = (
            self.result_root
            / "public"
            / f"{self.inputs.test_manifest}_use_receipt.json"
        )
        write_public_file(
            use_receipt,
            canonical_pretty_json_bytes(
                {
                    "schema_version": TEST_USE_RECEIPT_SCHEMA,
                    "protocol_id": PROTOCOL_ID,
                    "manifest": self.inputs.test_manifest,
                    "manifest_sha256": sha256_bytes(
                        (
                            self.inputs.manifest_root
                            / f"{self.inputs.test_manifest}_private_manifest.jsonl"
                        ).read_bytes()
                    ),
                    "reason": claim["reason"],
                    "maximum_uses": 1,
                    "global_ledger_claim_sha256": claim_sha256,
                    "started_at_utc": utc_now(),
                }
            ),
        )
        self._state["test_ledger_claim_sha256"] = claim_sha256
        self._write_state()
        for ordinal, task in enumerate(self.inputs.test):
            self._execute_test_session(
                control,
                task=task,
                context=None,
                logical_arm="control_test",
                executor_arm="control",
                task_ordinal=ordinal,
                round_index=0,
                stage="FINAL_TEST",
            )
            self._execute_test_session(
                online,
                task=task,
                context=frozen.contexts[task.category],
                logical_arm="online_test",
                executor_arm="online",
                task_ordinal=ordinal,
                round_index=0,
                stage="FINAL_TEST",
            )
        if self._formal_core_job_count() != jobs_before:
            raise SupervisedExperimentError("TEST_CREATED_EVOLUTION_JOB")
        completed = complete_test_manifest_use_v1(
            inputs=self.inputs,
            run_id=self.run_id,
            claim_entry_sha256=claim_sha256,
            completed_at_utc=utc_now(),
        )
        self._state["test_ledger_completion_sha256"] = completed["entry_sha256"]
        self._write_state()

    def _execute_test_session(
        self,
        executor: LocalCodexCLIExecutor,
        **arguments: object,
    ) -> SessionOutcomeV1:
        """Retry only pre-completion Test infrastructure attempts in the active run."""

        for infrastructure_attempt in range(1, _TEST_INFRASTRUCTURE_ATTEMPTS + 1):
            try:
                return self._execute_session(
                    executor,
                    infrastructure_attempt=infrastructure_attempt,
                    **arguments,  # type: ignore[arg-type]
                )
            except LocalCodexExecutionError as exc:
                if (
                    exc.completion_observed
                    or not exc.retry_allowed
                    or infrastructure_attempt == _TEST_INFRASTRUCTURE_ATTEMPTS
                ):
                    raise
        raise AssertionError("unreachable Test infrastructure attempt loop")

    def _execute_session(
        self,
        executor: LocalCodexCLIExecutor,
        *,
        task: PrivateChemBench4KTask,
        context: CoreResolvedSupervisedContextV1 | None,
        logical_arm: str,
        executor_arm: Literal["control", "online"],
        task_ordinal: int,
        round_index: Literal[0, 1, 2],
        stage: str,
        checkpoint: int | None = None,
        infrastructure_attempt: int = 1,
    ) -> SessionOutcomeV1:
        self._require_session_admission(
            task=task,
            context=context,
            logical_arm=logical_arm,
            task_ordinal=task_ordinal,
            round_index=round_index,
            stage=stage,
        )
        if infrastructure_attempt < 1:
            raise ValueError("infrastructure_attempt must be positive")
        session_id = self._session_id(
            stage,
            logical_arm,
            task,
            round_index,
            infrastructure_attempt,
        )
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
            "infrastructure_attempt": infrastructure_attempt,
            "prompt_sha256": sha256_bytes(prompt.text.encode("utf-8")),
            "started_at_utc": utc_now(),
        }
        self._append_private(attempt_event)
        try:
            attempt = executor.execute_supervised(
                SupervisedAgentRequestV1(
                    rendered_public_prompt=prompt.text,
                    resolved_context=context,
                    session_id=session_id,
                    arm=executor_arm,
                    task_ordinal=task_ordinal,
                    round_index=round_index,
                    run_id=self.run_id,
                    task_uid=task.uid,
                )
            )
            binding = executor.consume_supervised_context_receipt(session_id)
            binding.require_match()
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
            "infrastructure_attempt": infrastructure_attempt,
            "official_prediction": evaluation.official.prediction,
            "official_parse_status": evaluation.official.status.value,
            "strict_prediction": evaluation.strict.prediction,
            "strict_parse_status": evaluation.strict.status.value,
            "memory_artifact_id": (
                None if context is None else context.memory.core_artifact_id
            ),
            "skill_artifact_id": (
                None if context is None else context.skill.core_artifact_id
            ),
            "agent_system_artifact_id": (
                None if context is None else context.agent_system.core_artifact_id
            ),
            "context_binding_sha256": binding.digest,
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
            context_binding=binding.to_public_dict(),
        )

    def _require_session_admission(
        self,
        *,
        task: PrivateChemBench4KTask,
        context: CoreResolvedSupervisedContextV1 | None,
        logical_arm: str,
        task_ordinal: int,
        round_index: int,
        stage: str,
    ) -> None:
        if self._state["stage"] != stage:
            raise SupervisedExperimentError("SESSION_STAGE_BINDING_INVALID")
        allowed_arms = {
            "SUPERVISED_UPDATE_SMOKE": {"online_smoke"},
            "ONLINE_CANARY": {"online_canary"},
            "CONTROL_CANARY": {"control_canary"},
            "PROBE_SMOKE": {"control_probe_smoke", "online_probe_smoke"},
            "CONTROL_TRAIN": {"control_train"},
            "ONLINE_TRAIN": {"online_train"},
            "FINAL_TEST": {"control_test", "online_test"},
        }
        expected_partition = self.inputs.train
        if stage.startswith("PROBE_CHECKPOINT_"):
            allowed_arms = {**allowed_arms, stage: {"control_probe", "online_probe"}}
            expected_partition = self.inputs.probe
        elif stage == "PROBE_SMOKE":
            expected_partition = self.inputs.probe
        elif stage == "FINAL_TEST":
            expected_partition = self.inputs.test
        if logical_arm not in allowed_arms.get(stage, set()):
            raise SupervisedExperimentError("SESSION_ARM_STAGE_INVALID")
        if task.uid not in {item.uid for item in expected_partition}:
            raise SupervisedExperimentError("SESSION_PARTITION_SCOPE_INVALID")
        if logical_arm.startswith("control") and context is not None:
            raise SupervisedExperimentError("CONTROL_CONTEXT_INJECTION_INVALID")
        if logical_arm == "online_test" and context is None:
            raise SupervisedExperimentError("TEST_FROZEN_CONTEXT_MISSING")
        if (
            logical_arm == "online_probe"
            and stage != "PROBE_CHECKPOINT_00"
            and context is None
        ):
            raise SupervisedExperimentError("PROBE_FROZEN_CONTEXT_MISSING")
        if logical_arm in {"online_train", "online_canary"} and (
            task_ordinal > 0 or round_index > 0
        ) and context is None:
            raise SupervisedExperimentError("ONLINE_CONTEXT_MISSING")
        if (
            stage == "CONTROL_TRAIN"
            and int(self._state["task_sessions"]) == 0
            and (task_ordinal != 0 or round_index != 0)
        ):
            raise SupervisedExperimentError("FORMAL_FIRST_SESSION_INVALID")

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
        predecessor: CoreResolvedSupervisedContextV1 | None,
    ) -> SupervisedEvolutionPacketV1:
        return SupervisedEvolutionPacketV1.from_evaluation(
            task=task,
            training_task_ordinal=category_ordinal + 1,
            round_index=round_index,
            evaluation=outcome.evaluation,
            predecessor_memory=(
                None if predecessor is None else predecessor.memory.markdown
            ),
            predecessor_artifact_id=(
                None if predecessor is None else predecessor.memory.core_artifact_id
            ),
            session_id=outcome.session_id,
            predecessor_skill=(
                None if predecessor is None else predecessor.skill.markdown
            ),
            predecessor_skill_artifact_id=(
                None if predecessor is None else predecessor.skill.core_artifact_id
            ),
            predecessor_agent_system=(
                None if predecessor is None else predecessor.agent_system.markdown
            ),
            predecessor_agent_system_artifact_id=(
                None
                if predecessor is None
                else predecessor.agent_system.core_artifact_id
            ),
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
                "auxiliary_targets": [
                    {
                        "target_id": artifact.target_id,
                        "method_id": artifact.method_id,
                        "method_descriptor_sha256": artifact.method_descriptor_digest,
                        "job_id": artifact.job_id,
                        "artifact_id": artifact.core_artifact_id,
                        "artifact_payload_sha256": artifact.artifact_payload_sha256,
                        "context_resolution_sha256": (
                            artifact.context_resolution_digest
                        ),
                        "inspection": artifact.inspection.to_public_payload(),
                    }
                    for artifact in result.supervised_auxiliary_artifacts
                ],
                "completed_at_utc": utc_now(),
            }
        )
        self._state["reflector_completions"] = (
            int(self._state["reflector_completions"]) + 1
        )
        target_count = 1 + len(result.supervised_auxiliary_artifacts)
        if target_count != 3:
            raise SupervisedExperimentError("CORE_TARGET_SET_INCOMPLETE")
        self._state["core_jobs"] = int(self._state["core_jobs"]) + target_count
        self._state["core_artifacts"] = (
            int(self._state["core_artifacts"]) + target_count
        )
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
        infrastructure_attempt: int,
    ) -> str:
        ordinal = int(self._state["task_sessions"]) + 1
        identity = hashlib.sha256(
            (
                f"{self.run_id}:{stage}:{arm}:{task.uid}:{round_index}:"
                f"{ordinal}:{infrastructure_attempt}"
            ).encode()
        ).hexdigest()[:20]
        return (
            f"st-{ordinal:06d}-{round_index}-infra{infrastructure_attempt}-{identity}"
        )

    def _set_stage(self, stage: str) -> None:
        current = str(self._state["stage"])
        if stage not in _STAGE_TRANSITIONS.get(current, frozenset()):
            raise SupervisedExperimentError("STAGE_TRANSITION_INVALID")
        if current == "INITIALIZED":
            expected = (
                "SUPERVISED_UPDATE_SMOKE"
                if self.run_mode == "preflight"
                else "CONTROL_TRAIN"
            )
            if stage != expected:
                raise SupervisedExperimentError("RUN_MODE_STAGE_INVALID")
        self._require_stage_counters(stage)
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

    def _require_stage_counters(self, stage: str) -> None:
        sessions = int(self._state["task_sessions"])
        reflectors = int(self._state["reflector_completions"])
        jobs = int(self._state["core_jobs"])
        artifacts = int(self._state["core_artifacts"])
        if stage == "PROBE_CHECKPOINT_00" and (
            sessions,
            reflectors,
            jobs,
            artifacts,
        ) != (73, 19, PREFLIGHT_CORE_JOBS, PREFLIGHT_CORE_ARTIFACTS):
            raise SupervisedExperimentError("PREFLIGHT_STAGE_COUNTER_INVALID")
        if stage == "ONLINE_CORE_INITIALIZATION" and (
            sessions,
            reflectors,
            jobs,
            artifacts,
        ) != (
            1_350,
            0,
            0,
            0,
        ):
            raise SupervisedExperimentError("CONTROL_TRAIN_NOT_COMPLETE")
        if stage.startswith("PROBE_CHECKPOINT_") and stage != "PROBE_CHECKPOINT_00":
            checkpoint = int(stage.rsplit("_", 1)[1])
            expected_sessions = 1_350 + (27 * checkpoint) + (
                180 * ((checkpoint // 10) - 1)
            )
            expected_core = 54 * checkpoint
            if (sessions, reflectors, jobs, artifacts) != (
                expected_sessions,
                18 * checkpoint,
                expected_core,
                expected_core,
            ):
                raise SupervisedExperimentError("ONLINE_TRAIN_CHECKPOINT_COUNTER_INVALID")
        if stage == "FREEZE_FINAL_CONTEXT" and (
            sessions,
            reflectors,
            jobs,
            artifacts,
        ) != (3_600, 900, FORMAL_CORE_JOBS, FORMAL_CORE_ARTIFACTS):
            raise SupervisedExperimentError("FORMAL_TRAIN_PROBE_NOT_COMPLETE")
        if stage == "REPORTING" and (sessions, reflectors, jobs, artifacts) != (
            FORMAL_TASK_CALLS,
            FORMAL_REFLECTOR_CALLS,
            FORMAL_CORE_JOBS,
            FORMAL_CORE_ARTIFACTS,
        ):
            raise SupervisedExperimentError("FINAL_TEST_NOT_COMPLETE")

    def _fail_closed(self, exc: BaseException) -> None:
        failure_code = (
            exc.finding_code
            if isinstance(exc, SupervisedExperimentError)
            else _closed_exception_code(exc)
        )
        self._state["status"] = "FAIL_CLOSED"
        self._state["failure_code"] = failure_code
        if "ARTIFACT" in failure_code:
            self._state["artifact_findings"] = int(self._state["artifact_findings"]) + 1
        if "CONTEXT" in failure_code:
            self._state["context_findings"] = int(self._state["context_findings"]) + 1
        if "SECURITY" in failure_code:
            self._state["security_findings"] = int(self._state["security_findings"]) + 1
        self._state["failed_at_utc"] = utc_now()
        self._write_state()

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
    taskwise_code = getattr(exc, "taskwise_failure_code", None)
    if type(taskwise_code) is str and re.fullmatch(r"[A-Z][A-Z0-9_]{2,95}", taskwise_code):
        return taskwise_code
    closed_message = str(exc)
    if re.fullmatch(r"[A-Z][A-Z0-9_]{2,95}", closed_message):
        return closed_message
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
    "FrozenContextSetV1",
    "SupervisedExperimentError",
    "SupervisedTransferExperimentV1",
    "build_complete_dry_run_v1",
    "load_experiment_inputs_v1",
]
