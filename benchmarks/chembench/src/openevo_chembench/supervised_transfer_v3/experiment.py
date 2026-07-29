"""Three-answer, two-cycle, evolved-only supervised transfer v3 controller."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from openevo.runtime.managed import MANAGED_RUNTIME_RELEASES

from openevo_chembench.chembench4k_dataset import ChemBench4KDatasetLoader
from openevo_chembench.chembench4k_models import CHEMBENCH4K_CATEGORIES, PrivateChemBench4KTask
from openevo_chembench.supervised_transfer_v1.common import (
    canonical_json_bytes,
    canonical_pretty_json_bytes,
    sha256_bytes,
    write_public_file,
)
from openevo_chembench.supervised_transfer_v2 import experiment as v2
from openevo_chembench.supervised_transfer_v2.artifacts import CoreResolvedSupervisedContextV2
from openevo_chembench.supervised_transfer_v2.core import (
    TaskwiseCoreEvolutionBridgeV1,
    TaskwiseCoreUpdateResultV1,
    build_supervised_core_bridge_at_roots_v2,
)
from openevo_chembench.supervised_transfer_v2.executor import (
    SupervisedManagedCodexExecutorV2,
    SupervisedTaskExecutionCodeV2,
    SupervisedTaskExecutionErrorV2,
)
from openevo_chembench.supervised_transfer_v2.experiment import (
    ExperimentInputsV2,
    TaskExecutorV2,
)
from openevo_chembench.supervised_transfer_v2.managed_codex import (
    codex_subscription_auth_source_v2,
    load_managed_candidate_codex_v2,
    load_managed_codex_v2,
)
from openevo_chembench.supervised_transfer_v2.prepare import verify_phase0_v2
from openevo_chembench.supervised_transfer_v2.runtime_services import load_runtime_services_v2
from openevo_chembench.supervised_transfer_v2.split import load_private_partition_v2
from openevo_chembench.supervised_transfer_v3.config import (
    EVOLUTION_CYCLES,
    PROTOCOL_ID,
    TARGETS,
    TEST_COUNT,
    TRAIN_COUNT,
    TRAIN_ROUNDS,
    V2_MANIFEST_ROOT,
    SupervisedTransferConfigV3,
)
from openevo_chembench.supervised_transfer_v3.source_identity import verify_source_manifest_v3
from openevo_chembench.supervised_transfer_v3.split_reference import (
    verify_split_reference_receipt_v3,
)
from openevo_chembench.supervised_transfer_v3.test_ledger import (
    FinalTestConsumptionLedgerV3,
)

RUN_STATE_SCHEMA = "ChemBenchSupervisedTransferRunStateV3"
PAID_PLAN_SCHEMA = "PlannedPaidExecutionReceiptV3"
SMOKE_AUTHORITY_SCHEMA = "SupervisedTransferSmokeAuthorityV3"
FROZEN_RECEIPT_SCHEMA = "FrozenThreeTargetTransferReceiptV3"
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}\Z", re.ASCII)
_TRANSITIONS = {
    "INITIALIZED": frozenset({"SMOKE_ONLINE", "ONLINE_CORE_INITIALIZATION"}),
    "SMOKE_ONLINE": frozenset({"SMOKE_COMPLETED"}),
    "ONLINE_CORE_INITIALIZATION": frozenset({"ONLINE_TRAIN"}),
    "ONLINE_TRAIN": frozenset({"ONLINE_TRAIN", "FREEZE_THREE_TARGETS"}),
    "FREEZE_THREE_TARGETS": frozenset({"EVOLVED_TEST"}),
    "EVOLVED_TEST": frozenset({"REPORTING"}),
    "REPORTING": frozenset({"COMPLETED"}),
}


class SupervisedExperimentV3Error(RuntimeError):
    def __init__(self, finding_code: str) -> None:
        if re.fullmatch(r"[A-Z][A-Z0-9_]{2,95}", finding_code) is None:
            raise ValueError("invalid v3 finding code")
        self.finding_code = finding_code
        super().__init__(finding_code)


@dataclass(frozen=True, slots=True)
class FrozenThreeTargetSetV3:
    contexts: dict[str, CoreResolvedSupervisedContextV2]
    artifact_set_digest: str
    receipt_sha256: str


def load_experiment_inputs_v3(
    repository_root: Path,
    config: SupervisedTransferConfigV3,
    *,
    require_runtime: bool,
) -> ExperimentInputsV2:
    if not isinstance(repository_root, Path) or not repository_root.is_absolute():
        raise TypeError("repository root must be absolute")
    repository = repository_root.resolve(strict=True)
    phase0 = verify_phase0_v2(repository)
    split_reference = verify_split_reference_receipt_v3(repository)
    if (
        phase0["status"] != "PASS"
        or phase0["test_actual_exposure_count"] != 0
        or split_reference["test_historical_exposure"] != 0
    ):
        raise SupervisedExperimentV3Error("SPLIT_ISOLATION_INVALID")
    snapshot = (repository / config.payload["dataset"]["root"]).resolve(strict=True)
    loader = ChemBench4KDatasetLoader(
        snapshot_root=snapshot,
        manifest_path=snapshot / "chembench4k_dataset_manifest_v2.json",
    )
    manifests = (repository / V2_MANIFEST_ROOT).resolve(strict=True)
    train = load_private_partition_v2(
        loader,
        partition="train",
        path=manifests / "train_private_manifest.jsonl",
    )
    test = load_private_partition_v2(
        loader,
        partition="test",
        path=manifests / "test_private_manifest.jsonl",
    )
    v2._require_partition_identity(train, test)
    if v2._git(repository, "diff", "--", "src/openevo"):
        raise SupervisedExperimentV3Error("SRC_OPENEVO_NOT_PRISTINE")
    source = verify_source_manifest_v3(repository)
    managed = load_managed_codex_v2(repository_root=repository) if require_runtime else None
    candidate = (
        load_managed_candidate_codex_v2(repository_root=repository) if require_runtime else None
    )
    runtime = load_runtime_services_v2(repository_root=repository) if require_runtime else None
    return ExperimentInputsV2(
        repository_root=repository,
        config=config,  # type: ignore[arg-type] -- v3 intentionally reuses the v2 execution ABI.
        loader=loader,
        train=train,
        test=test,
        manifest_root=manifests,
        split_receipt_sha256=str(split_reference["sha256"]),
        split_summary_sha256=str(split_reference["source_split_summary"]),
        source_manifest_sha256=str(source["manifest_sha256"]),
        source_commit=v2._git(repository, "rev-parse", "HEAD"),
        managed_codex=managed,
        candidate_codex=candidate,
        runtime_services=runtime,
    )


def build_complete_dry_run_v3(inputs: ExperimentInputsV2) -> dict[str, object]:
    prompts: dict[str, str] = {}
    demos: dict[str, tuple[str, ...]] = {}
    for task in (*inputs.train, *inputs.test):
        rendered = v2._render_prompt(inputs.loader, task)
        prompts[task.uid] = sha256_bytes(rendered.text.encode())
        demos[task.category] = rendered.demonstration_uids
    if len(prompts) != TRAIN_COUNT + TEST_COUNT:
        raise SupervisedExperimentV3Error("DRY_RUN_PROMPT_COUNT_INVALID")
    schedule = [
        {
            "category": category,
            "task_count": len(v2._by_category(inputs.train)[category]),
            "sessions_per_task": TRAIN_ROUNDS,
            "cycles_per_task": EVOLUTION_CYCLES,
            "sequence": ["ROUND_0", "CYCLE_1", "ROUND_1", "CYCLE_2", "ROUND_2_FINAL"],
            "targets_per_cycle": list(TARGETS),
        }
        for category in CHEMBENCH4K_CATEGORIES
    ]
    if any(item["task_count"] != 50 for item in schedule):
        raise SupervisedExperimentV3Error("DRY_RUN_TRAIN_SCHEDULE_INVALID")
    return {
        "schema_version": "ChemBenchSupervisedTransferDryRunV3",
        "protocol_id": PROTOCOL_ID,
        "status": "PASS",
        "model_calls": 0,
        "stage_order": ["ONLINE_TRAIN", "FREEZE_THREE_TARGETS", "EVOLVED_TEST", "REPORTING"],
        "control_train_enabled": False,
        "control_test_enabled": False,
        "probe_enabled": False,
        "train_schedule": schedule,
        "planned_paid_calls": inputs.config.call_budget,
        "train_order_sha256": v2._uid_order_digest(inputs.train),
        "test_order_sha256": v2._uid_order_digest(inputs.test),
        "prompt_binding_sha256": sha256_bytes(canonical_json_bytes(prompts)),
        "demonstration_binding_sha256": sha256_bytes(
            canonical_json_bytes({key: list(value) for key, value in demos.items()})
        ),
        "split_reference_sha256": inputs.split_receipt_sha256,
        "source_manifest_sha256": inputs.source_manifest_sha256,
        "source_commit": inputs.source_commit,
        "candidate_execution_path": "TaskRequest->Rollout->Gateway->CodexHarness",
        "reflector_execution_path": "Core planned job->worker->codex_cli provider",
        "test_evolution_jobs": 0,
    }


class SupervisedTransferExperimentV3(v2.SupervisedTransferExperimentV2):
    """V3 controller reusing the verified v2 execution and Core artifact ABIs."""

    def __init__(
        self,
        *,
        inputs: ExperimentInputsV2,
        run_id: str,
        run_mode: Literal["smoke", "formal_online"],
        smoke_run_id: str | None = None,
        executor_factory: Callable[[Literal["control", "online"]], TaskExecutorV2] | None = None,
        bridge_factory: Callable[[Path, str], TaskwiseCoreEvolutionBridgeV1] | None = None,
    ) -> None:
        if run_mode not in {"smoke", "formal_online"}:
            raise ValueError("invalid v3 run mode")
        if run_mode == "formal_online" and (
            type(smoke_run_id) is not str or _RUN_ID.fullmatch(smoke_run_id) is None
        ):
            raise ValueError("formal v3 requires a smoke authority")
        if run_mode == "smoke" and smoke_run_id is not None:
            raise ValueError("smoke cannot consume a smoke authority")
        self.v3_run_mode = run_mode
        self.smoke_run_id = smoke_run_id
        super().__init__(
            inputs=inputs,
            run_id=run_id,
            run_mode=("contract_canary" if run_mode == "smoke" else "preflight"),
            executor_factory=executor_factory,
            bridge_factory=bridge_factory,
        )
        self.run_mode = run_mode  # type: ignore[assignment]
        self._state.update(
            {
                "schema_version": RUN_STATE_SCHEMA,
                "protocol_id": PROTOCOL_ID,
                "run_mode": run_mode,
                "smoke_run_id": smoke_run_id,
                "control_train_enabled": False,
                "control_test_enabled": False,
                "probe_enabled": False,
                "attempts_per_train_task": TRAIN_ROUNDS,
                "evolution_cycles_per_train_task": EVOLUTION_CYCLES,
            }
        )
        self._write_state()

    def run_smoke(self) -> dict[str, object]:
        if self.v3_run_mode != "smoke":
            raise SupervisedExperimentV3Error("RUN_MODE_MISMATCH")
        try:
            with ExitStack() as stack:
                executor = v2._enter_if_context(stack, self._executor_factory("online"))
                self._set_stage("SMOKE_ONLINE")
                task = v2._by_category(self.inputs.train)["Temperature_Prediction"][0]
                bridge = stack.enter_context(
                    self._bridge_factory(
                        self.state_root / "private/core/smoke/Temperature_Prediction",
                        "Temperature_Prediction",
                    )
                )
                self._run_one_online_task_v3(
                    executor,
                    task=task,
                    category_ordinal=0,
                    bridge=bridge,
                    stage="SMOKE_ONLINE",
                    persist_head=False,
                )
            self._require_counts(task_sessions=3, reflector_calls=2, core_jobs=6)
            self._set_stage("SMOKE_COMPLETED")
            authority = self._smoke_authority()
            path = self.result_root / "public/smoke_authority_v3.json"
            write_public_file(path, canonical_pretty_json_bytes(authority))
            self._state["smoke_authority_sha256"] = sha256_bytes(path.read_bytes())
            self._state["status"] = "SMOKE_COMPLETED"
            self._state["completed_at_utc"] = v2.utc_now()
            self._write_state()
            return authority
        except BaseException as exc:
            self._fail_closed(exc)
            raise

    def run_formal_online(self) -> dict[str, object]:
        if self.v3_run_mode != "formal_online":
            raise SupervisedExperimentV3Error("RUN_MODE_MISMATCH")
        try:
            self._verify_smoke_authority()
            with ExitStack() as stack:
                executor = v2._enter_if_context(stack, self._executor_factory("online"))
                self._open_formal_bridges(stack)
                self._run_online_train_v3(executor)
                self._require_counts(task_sessions=1350, reflector_calls=900, core_jobs=2700)
                frozen = self._freeze_three_targets_v3()
                self._run_evolved_test_v3(frozen, executor)
            if int(self._state["task_sessions"]) != 1800:
                raise SupervisedExperimentV3Error("FINAL_TASK_COUNTER_MISMATCH")
        except BaseException as exc:
            self._fail_closed(exc)
            raise
        self._set_stage("REPORTING")
        self._state["status"] = "EXPERIMENT_COMPLETED_REPORTING"
        self._state["test_completed_at_utc"] = v2.utc_now()
        self._write_state()
        try:
            from openevo_chembench.supervised_transfer_v3.reporting import (
                build_reports_v3,
                export_final_html_to_windows_desktop_v3,
            )

            report = build_reports_v3(
                repository_root=self.inputs.repository_root,
                result_root=self.result_root,
                state_root=self.state_root,
                run_id=self.run_id,
            )
            desktop = export_final_html_to_windows_desktop_v3(
                report_html=Path(str(report["report_root"])) / "final_report.html",
                source_commit=self.inputs.source_commit,
                utc_token=datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
            )
            report["desktop_report"] = desktop
            self._set_stage("COMPLETED")
            self._state.update(
                {
                    "status": "COMPLETED",
                    "completed_at_utc": v2.utc_now(),
                    "final_report_sha256": report["final_report_sha256"],
                    "desktop_report_sha256": desktop["sha256"],
                }
            )
            self._write_state()
            return report
        except BaseException as exc:
            self._state.update(
                {
                    "status": "REPORTING_FAILED",
                    "reporting_failure_code": v2._failure_code(exc),
                    "reporting_failed_at_utc": v2.utc_now(),
                }
            )
            self._write_state()
            raise

    def _run_online_train_v3(self, executor: TaskExecutorV2) -> None:
        self._set_stage("ONLINE_TRAIN")
        by_category = v2._by_category(self.inputs.train)
        for category in CHEMBENCH4K_CATEGORIES:
            bridge = self._bridges[category]
            for category_ordinal, task in enumerate(by_category[category]):
                self._run_one_online_task_v3(
                    executor,
                    task=task,
                    category_ordinal=category_ordinal,
                    bridge=bridge,
                    stage="ONLINE_TRAIN",
                    persist_head=True,
                )

    def _run_one_online_task_v3(
        self,
        executor: TaskExecutorV2,
        *,
        task: PrivateChemBench4KTask,
        category_ordinal: int,
        bridge: TaskwiseCoreEvolutionBridgeV1,
        stage: str,
        persist_head: bool,
    ) -> TaskwiseCoreUpdateResultV1:
        head = self._heads[task.category] if persist_head else None
        trajectories = []
        evaluations = []
        context = None if head is None else bridge.issue_runtime_context(head)
        for round_index in range(TRAIN_ROUNDS):
            outcome = self._execute_session(
                executor,
                task=task,
                context=context,
                logical_arm=("online_train" if stage == "ONLINE_TRAIN" else "online_smoke"),
                executor_arm="online",
                task_ordinal=category_ordinal,
                round_index=round_index,
                stage=stage,
            )
            evaluations.append(outcome.evaluation)
            if round_index == TRAIN_ROUNDS - 1:
                break
            trajectory = self._trajectory(task, category_ordinal, round_index, outcome)
            trajectories.append(trajectory)
            head = bridge.apply_update(
                self._update_request(
                    task=task,
                    category_ordinal=category_ordinal,
                    round_index=round_index,
                    trajectories=tuple(trajectories),
                    evaluations=tuple(evaluations),
                    predecessor=head,
                    predecessor_context=context,
                )
            )
            context = bridge.issue_runtime_context(head)
            self._record_core_result(stage, task.category, head)
        if head is None or head.update_index != EVOLUTION_CYCLES:
            raise SupervisedExperimentV3Error("ONLINE_TASK_FINAL_HEAD_MISSING")
        if persist_head:
            self._heads[task.category] = head
        return head

    def _freeze_three_targets_v3(self) -> FrozenThreeTargetSetV3:
        self._set_stage("FREEZE_THREE_TARGETS")
        contexts: dict[str, CoreResolvedSupervisedContextV2] = {}
        categories: dict[str, dict[str, object]] = {}
        for category in CHEMBENCH4K_CATEGORIES:
            head = self._heads[category]
            if head is None or head.global_update_ordinal != 100 or head.update_index != 2:
                raise SupervisedExperimentV3Error("FINAL_CATEGORY_HEAD_INCOMPLETE")
            bridge = self._bridges[category]
            bridge.verify_update_result(head)
            context = bridge.issue_runtime_context(head)
            contexts[category] = context
            categories[category] = {
                "global_update_ordinal": head.global_update_ordinal,
                "final_task_ordinal": 50,
                "final_cycle": 2,
                "text_memory": v2._target_receipt(context.memory),
                "skill_bundle": v2._target_receipt(context.skill),
                "agent_system": v2._target_receipt(context.agent_system),
                "context_binding_sha256": sha256_bytes(
                    canonical_json_bytes(context.to_runtime_payload())
                ),
            }
        artifact_set_digest = sha256_bytes(canonical_json_bytes(categories))
        receipt = {
            "schema_version": FROZEN_RECEIPT_SCHEMA,
            "protocol_id": PROTOCOL_ID,
            "source_commit": self.inputs.source_commit,
            "config_sha256": self.inputs.config.digest,
            "split_reference_sha256": self.inputs.split_receipt_sha256,
            "source_manifest_sha256": self.inputs.source_manifest_sha256,
            "model": self.inputs.config.model,
            "candidate_codex_sha256": self.inputs.candidate_codex.executable_sha256,
            "reflector_codex_sha256": self.inputs.managed_codex.executable_sha256,
            "runtime_services_identity_sha256": self.inputs.runtime_services.digest,
            "test_order_sha256": v2._uid_order_digest(self.inputs.test),
            "category_count": len(categories),
            "target_count": len(categories) * len(TARGETS),
            "artifact_set_sha256": artifact_set_digest,
            "categories": categories,
            "checkpoint_selection": "FINAL_TASK_50_CYCLE_2_ONLY",
            "test_evolution_allowed": False,
            "frozen_at_utc": v2.utc_now(),
        }
        path = self.result_root / "public/frozen_three_target_transfer_receipt_v3.json"
        write_public_file(path, canonical_pretty_json_bytes(receipt))
        receipt_sha256 = sha256_bytes(path.read_bytes())
        self._state["frozen_artifact_set_sha256"] = artifact_set_digest
        self._state["frozen_receipt_sha256"] = receipt_sha256
        self._write_state()
        return FrozenThreeTargetSetV3(contexts, artifact_set_digest, receipt_sha256)

    def _run_evolved_test_v3(
        self,
        frozen: FrozenThreeTargetSetV3,
        executor: TaskExecutorV2,
    ) -> None:
        self._set_stage("EVOLVED_TEST")
        model_digest = sha256_bytes(
            canonical_json_bytes(
                {
                    "model": self.inputs.config.model,
                    "reasoning_effort": self.inputs.config.reasoning_effort,
                    "managed_runtime_image": MANAGED_RUNTIME_RELEASES["managed_science"].loaded_image_id,
                }
            )
        )
        ledger = FinalTestConsumptionLedgerV3(
            path=(
                self.inputs.repository_root
                / self.inputs.config.payload["roots"]["state"]
                / "private/final_test_consumption_ledger_v3.jsonl"
            ).resolve(),
            source_commit=self.inputs.source_commit,
            artifact_set_digest=frozen.artifact_set_digest,
            config_digest=self.inputs.config.digest,
            model_digest=model_digest,
        )
        for ordinal, task in enumerate(self.inputs.test):
            retry_window_started_at = time.monotonic()
            for attempt_number in range(1, v2._FINAL_TEST_INFRASTRUCTURE_RETRY_LIMIT + 1):
                if attempt_number > 1:
                    v2._require_executor_retry_window(retry_window_started_at)
                self._require_source_frozen()
                context = frozen.contexts[task.category]
                attempt_id = self._session_id(
                    "EVOLVED_TEST", "evolved", task, 0, attempt_number
                )
                ledger.claim(task_uid=task.uid, attempt_id=attempt_id, timestamp=v2.utc_now())
                try:
                    self._execute_session(
                        executor,
                        task=task,
                        context=context,
                        logical_arm="test_evolved",
                        executor_arm="online",
                        task_ordinal=ordinal,
                        round_index=0,
                        stage="EVOLVED_TEST",
                        session_id_override=attempt_id,
                        completion_callback=lambda completion, *, _task=task, _attempt=attempt_id: ledger.complete(
                            task_uid=_task.uid,
                            attempt_id=_attempt,
                            completion=completion,
                            timestamp=v2.utc_now(),
                        ),
                    )
                    break
                except SupervisedTaskExecutionErrorV2 as exc:
                    if exc.completion_exists is True:
                        if exc.completion_sha256 is None:
                            raise SupervisedExperimentV3Error(
                                "TEST_COMPLETION_DIGEST_MISSING"
                            ) from exc
                        ledger.complete_digest(
                            task_uid=task.uid,
                            attempt_id=attempt_id,
                            completion_sha256=exc.completion_sha256,
                            timestamp=v2.utc_now(),
                        )
                        raise
                    retryable = (
                        exc.code is SupervisedTaskExecutionCodeV2.TASK_FAILED
                        and exc.completion_exists is False
                        and attempt_number < v2._FINAL_TEST_INFRASTRUCTURE_RETRY_LIMIT
                    )
                    if not retryable:
                        raise
                    delay = v2._executor_retry_delay_seconds(
                        retry_window_started_at,
                        v2._FINAL_TEST_INFRASTRUCTURE_RETRY_SECONDS,
                    )
                    self._append_public(
                        {
                            "schema_version": "FinalTestInfrastructureRetryPublicV3",
                            "kind": "INFRASTRUCTURE_RETRY_NO_COMPLETION",
                            "stage": "EVOLVED_TEST",
                            "task_uid": task.uid,
                            "attempt_number": attempt_number,
                            "same_source_config_artifact_model": True,
                            "completion_exists": False,
                            "retry_after_seconds": delay,
                            "recorded_at_utc": v2.utc_now(),
                        }
                    )
                    time.sleep(delay)
            else:
                raise SupervisedExperimentV3Error("FINAL_TEST_INFRASTRUCTURE_RETRY_EXHAUSTED")
        summary = ledger.summary()
        if summary["completion_count"] != TEST_COUNT:
            raise SupervisedExperimentV3Error("FINAL_TEST_LEDGER_INCOMPLETE")
        write_public_file(
            self.result_root / "public/final_test_ledger_receipt_v3.json",
            canonical_pretty_json_bytes(summary),
        )

    def _execute_single_session(
        self,
        executor: TaskExecutorV2,
        *,
        task: PrivateChemBench4KTask,
        context: CoreResolvedSupervisedContextV2 | None,
        logical_arm: str,
        executor_arm: Literal["control", "online"],
        task_ordinal: int,
        round_index: Literal[0, 1, 2, 3],
        stage: str,
        session_id: str,
        completion_callback: Callable[[str], object] | None,
    ) -> v2.SessionOutcomeV2:
        if executor_arm != "online" or logical_arm not in {
            "online_smoke",
            "online_train",
            "test_evolved",
        }:
            raise SupervisedExperimentV3Error("V3_EXECUTION_ARM_INVALID")
        if stage in {"SMOKE_ONLINE", "ONLINE_TRAIN"}:
            if round_index not in {0, 1, 2}:
                raise SupervisedExperimentV3Error("V3_TRAIN_ROUND_INVALID")
            expected_missing = task_ordinal == 0 and round_index == 0
            if (context is None) != expected_missing:
                raise SupervisedExperimentV3Error("V3_ONLINE_CONTEXT_SEQUENCE_INVALID")
        elif stage == "EVOLVED_TEST":
            if round_index != 0 or context is None or logical_arm != "test_evolved":
                raise SupervisedExperimentV3Error("V3_EVOLVED_TEST_CONTEXT_REQUIRED")
        else:
            raise SupervisedExperimentV3Error("V3_SESSION_STAGE_INVALID")
        return super()._execute_single_session(
            executor,
            task=task,
            context=context,
            logical_arm=logical_arm,
            executor_arm=executor_arm,
            task_ordinal=task_ordinal,
            round_index=round_index,
            stage=stage,
            session_id=session_id,
            completion_callback=completion_callback,
        )

    def _default_executor(self, arm: Literal["control", "online"]) -> TaskExecutorV2:
        if arm != "online":
            raise SupervisedExperimentV3Error("CONTROL_EXECUTOR_FORBIDDEN")
        return SupervisedManagedCodexExecutorV2(
            arm="online",
            timeout_seconds=self.inputs.config.task_timeout_seconds,
            rollout_url=self.inputs.config.rollout_url,
            runtime_services=self.inputs.runtime_services,
            protocol_id=PROTOCOL_ID,
        )

    def _default_bridge(self, state_root: Path, _category: str) -> TaskwiseCoreEvolutionBridgeV1:
        return build_supervised_core_bridge_at_roots_v2(
            state_root=state_root.resolve(),
            framework_lock=(
                self.inputs.repository_root / self.inputs.config.framework_lock
            ).resolve(strict=True),
            codex_executable=self.inputs.managed_codex.executable,
            auth_source=codex_subscription_auth_source_v2(),
            timeout_seconds=self.inputs.config.reflector_timeout_seconds,
            updates_per_task=EVOLUTION_CYCLES,
        )

    def _write_paid_plan(self) -> None:
        mode = self.v3_run_mode
        if mode == "smoke":
            calls = {
                "candidate_task_calls": 3,
                "reflector_calls": 2,
                "core_jobs": 6,
                "typed_artifacts": 6,
                "context_resolutions": 6,
                "candidate_subscription_readiness_calls_minimum": 3,
                "candidate_subscription_readiness_calls_maximum": 6,
                "answer_and_reflector_model_calls": 5,
                "total_model_calls": 8,
                "maximum_model_calls": 11,
            }
        else:
            calls = self.inputs.config.call_budget
        payload = {
            "schema_version": PAID_PLAN_SCHEMA,
            "protocol_id": PROTOCOL_ID,
            "run_id": self.run_id,
            "run_mode": mode,
            "smoke_run_id": self.smoke_run_id,
            "source_commit": self.inputs.source_commit,
            "config_sha256": self.inputs.config.digest,
            "split_reference_sha256": self.inputs.split_receipt_sha256,
            "source_manifest_sha256": self.inputs.source_manifest_sha256,
            "model": self.inputs.config.model,
            "reasoning_effort": self.inputs.config.reasoning_effort,
            "task_timeout_seconds": self.inputs.config.task_timeout_seconds,
            "reflector_timeout_seconds": self.inputs.config.reflector_timeout_seconds,
            "task_infrastructure_retry_limit": v2._TASK_INFRASTRUCTURE_RETRY_LIMIT,
            "final_test_infrastructure_retry_limit": v2._FINAL_TEST_INFRASTRUCTURE_RETRY_LIMIT,
            "stall_limit_seconds": v2._EXECUTOR_STALL_SECONDS,
            "attempts_per_train_task": TRAIN_ROUNDS,
            "evolution_cycles_per_train_task": EVOLUTION_CYCLES,
            "targets_per_cycle": list(TARGETS),
            "control_train_enabled": False,
            "control_test_enabled": False,
            "probe_enabled": False,
            "planned_calls": calls,
            "candidate_execution_path": "TaskRequest->Rollout->Gateway->CodexHarness",
            "reflector_execution_path": "Core planned job->worker->codex_cli provider",
            "candidate_codex_identity_sha256": self.inputs.candidate_codex.digest,
            "candidate_codex_executable_sha256": self.inputs.candidate_codex.executable_sha256,
            "reflector_codex_identity_sha256": self.inputs.managed_codex.digest,
            "reflector_codex_executable_sha256": self.inputs.managed_codex.executable_sha256,
            "runtime_services_identity_sha256": self.inputs.runtime_services.digest,
            "issued_at_utc": v2.utc_now(),
        }
        write_public_file(
            self.result_root / "public/planned_paid_execution_receipt_v3.json",
            canonical_pretty_json_bytes(payload),
        )

    def _smoke_authority(self) -> dict[str, object]:
        return {
            "schema_version": SMOKE_AUTHORITY_SCHEMA,
            "protocol_id": PROTOCOL_ID,
            "status": "PASS",
            "source_run_id": self.run_id,
            "source_commit": self.inputs.source_commit,
            "config_sha256": self.inputs.config.digest,
            "split_reference_sha256": self.inputs.split_receipt_sha256,
            "source_manifest_sha256": self.inputs.source_manifest_sha256,
            "candidate_codex_identity_sha256": self.inputs.candidate_codex.digest,
            "reflector_codex_identity_sha256": self.inputs.managed_codex.digest,
            "runtime_services_identity_sha256": self.inputs.runtime_services.digest,
            "category": "Temperature_Prediction",
            "task_sessions": 3,
            "reflector_calls": 2,
            "core_jobs": 6,
            "context_resolutions": 6,
            "text_memory_artifacts": 2,
            "skill_artifacts": 2,
            "agent_system_artifacts": 2,
            "security_findings": 0,
            "context_findings": 0,
            "artifact_findings": 0,
            "test_model_calls": 0,
            "artifacts_reusable": False,
            "issued_at_utc": v2.utc_now(),
        }

    def _verify_smoke_authority(self) -> None:
        path = (
            self.inputs.repository_root
            / self.inputs.config.payload["roots"]["results"]
            / "runs"
            / str(self.smoke_run_id)
            / "public/smoke_authority_v3.json"
        )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SupervisedExperimentV3Error("SMOKE_AUTHORITY_MISSING") from exc
        expected = {
            "schema_version": SMOKE_AUTHORITY_SCHEMA,
            "protocol_id": PROTOCOL_ID,
            "status": "PASS",
            "source_run_id": self.smoke_run_id,
            "source_commit": self.inputs.source_commit,
            "config_sha256": self.inputs.config.digest,
            "split_reference_sha256": self.inputs.split_receipt_sha256,
            "source_manifest_sha256": self.inputs.source_manifest_sha256,
            "candidate_codex_identity_sha256": self.inputs.candidate_codex.digest,
            "reflector_codex_identity_sha256": self.inputs.managed_codex.digest,
            "runtime_services_identity_sha256": self.inputs.runtime_services.digest,
            "category": "Temperature_Prediction",
            "task_sessions": 3,
            "reflector_calls": 2,
            "core_jobs": 6,
            "context_resolutions": 6,
            "text_memory_artifacts": 2,
            "skill_artifacts": 2,
            "agent_system_artifacts": 2,
            "security_findings": 0,
            "context_findings": 0,
            "artifact_findings": 0,
            "test_model_calls": 0,
            "artifacts_reusable": False,
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            raise SupervisedExperimentV3Error("SMOKE_AUTHORITY_INVALID")
        self._state["smoke_authority_sha256"] = sha256_bytes(path.read_bytes())
        self._write_state()

    def _require_counts(self, *, task_sessions: int, reflector_calls: int, core_jobs: int) -> None:
        if (
            self._state["task_sessions"] != task_sessions
            or self._state["reflector_calls"] != reflector_calls
            or self._state["core_jobs"] != core_jobs
            or self._state["context_resolutions"] != core_jobs
            or self._state["text_memory_artifacts"] != reflector_calls
            or self._state["skill_artifacts"] != reflector_calls
            or self._state["agent_system_artifacts"] != reflector_calls
            or self._state["security_findings"] != 0
            or self._state["context_findings"] != 0
            or self._state["artifact_findings"] != 0
        ):
            raise SupervisedExperimentV3Error("V3_COUNTER_MISMATCH")

    def _require_source_frozen(self) -> None:
        if (
            v2._git(self.inputs.repository_root, "rev-parse", "HEAD") != self.inputs.source_commit
            or v2._benchmark_status(self.inputs.repository_root)
            or v2._git(self.inputs.repository_root, "diff", "--", "src/openevo")
        ):
            raise SupervisedExperimentV3Error("TEST_SOURCE_IDENTITY_DRIFT")
        source_manifest = (
            self.inputs.repository_root
            / "benchmarks/chembench/manifests/supervised_transfer_v3/source_manifest_v3.json"
        )
        if sha256_bytes(source_manifest.read_bytes()) != self.inputs.source_manifest_sha256:
            raise SupervisedExperimentV3Error("TEST_SOURCE_MANIFEST_DRIFT")
        frozen = self.result_root / "public/frozen_three_target_transfer_receipt_v3.json"
        if sha256_bytes(frozen.read_bytes()) != self._state.get("frozen_receipt_sha256"):
            raise SupervisedExperimentV3Error("TEST_ARTIFACT_SET_DRIFT")

    def _session_id(
        self,
        stage: str,
        arm: str,
        task: PrivateChemBench4KTask,
        round_index: int,
        attempt: int,
    ) -> str:
        digest = sha256_bytes(
            canonical_json_bytes([self.run_id, stage, arm, task.uid, round_index, attempt])
        )
        return f"stv3-{stage.lower().replace('_', '-')[:18]}-{digest[:32]}"

    def _set_stage(self, stage: str) -> None:
        current = str(self._state["stage"])
        if stage != current and stage not in _TRANSITIONS.get(current, frozenset()):
            raise SupervisedExperimentV3Error("STAGE_TRANSITION_INVALID")
        self._state["stage"] = stage
        self._state["status"] = "COMPLETED" if stage == "COMPLETED" else "RUNNING"
        self._state["last_progress_utc"] = v2.utc_now()
        self._write_state()

    def _fail_closed(self, exc: BaseException) -> None:
        self._state.update(
            {
                "status": "FAIL_CLOSED",
                "failure_code": v2._failure_code(exc),
                "failed_at_utc": v2.utc_now(),
                "last_progress_utc": v2.utc_now(),
                "resume_allowed": False,
                "results_reusable": False,
            }
        )
        self._write_state()
        self._append_public(
            {
                "schema_version": "SupervisedTransferFailurePublicV3",
                "kind": "FAIL_CLOSED",
                "stage": self._state["stage"],
                "failure_code": self._state["failure_code"],
                "resume_allowed": False,
                "failed_at_utc": self._state["failed_at_utc"],
            }
        )


__all__ = [
    "FrozenThreeTargetSetV3",
    "SupervisedExperimentV3Error",
    "SupervisedTransferExperimentV3",
    "build_complete_dry_run_v3",
    "load_experiment_inputs_v3",
]
