"""Fail-closed four-round, three-target supervised transfer v2 controller."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterable
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from openevo.runtime.managed import MANAGED_RUNTIME_RELEASES

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
from openevo_chembench.models import RawAttempt
from openevo_chembench.supervised_transfer_v1.common import (
    canonical_json_bytes,
    canonical_pretty_json_bytes,
    sha256_bytes,
    write_private_file,
    write_public_file,
)
from openevo_chembench.supervised_transfer_v2.artifacts import (
    CoreResolvedAuxiliaryArtifactV2,
    CoreResolvedSupervisedContextV2,
)
from openevo_chembench.supervised_transfer_v2.config import (
    EVOLUTION_CYCLES,
    MANIFEST_ROOT,
    PROTOCOL_ID,
    TARGETS,
    TEST_COUNT,
    TRAIN_COUNT,
    TRAIN_ROUNDS,
    SupervisedTransferConfigV2,
)
from openevo_chembench.supervised_transfer_v2.context_binding import (
    SupervisedAgentRequestV2,
)
from openevo_chembench.supervised_transfer_v2.core import (
    TaskwiseCoreEvolutionBridgeV1,
    TaskwiseCoreUpdateRequestV1,
    TaskwiseCoreUpdateResultV1,
    _trajectory_forbidden_literals,
    build_supervised_core_bridge_at_roots_v2,
)
from openevo_chembench.supervised_transfer_v2.executor import (
    SupervisedManagedCodexExecutorV2,
    SupervisedTaskExecutionCodeV2,
    SupervisedTaskExecutionErrorV2,
)
from openevo_chembench.supervised_transfer_v2.managed_codex import (
    OpenEvoManagedCandidateCodexIdentityV2,
    OpenEvoManagedCodexIdentityV2,
    codex_subscription_auth_source_v2,
    load_managed_candidate_codex_v2,
    load_managed_codex_v2,
)
from openevo_chembench.supervised_transfer_v2.packet import (
    SupervisedEvolutionPacketV2,
)
from openevo_chembench.supervised_transfer_v2.prepare import verify_phase0_v2
from openevo_chembench.supervised_transfer_v2.runtime_services import (
    OpenEvoRuntimeServicesIdentityV2,
    load_runtime_services_v2,
)
from openevo_chembench.supervised_transfer_v2.source_identity import (
    verify_source_manifest_v2,
)
from openevo_chembench.supervised_transfer_v2.split import load_private_partition_v2
from openevo_chembench.supervised_transfer_v2.test_ledger import (
    FinalTestConsumptionLedgerV2,
)
from openevo_chembench.supervised_transfer_v2.trajectory import (
    SupervisedTrajectoryV2,
)
from openevo_chembench.taskwise_feedback_v1 import (
    safe_signal_from_private_evaluation,
)

DRY_RUN_SCHEMA = "ChemBenchSupervisedTransferDryRunV2"
RUN_STATE_SCHEMA = "ChemBenchSupervisedTransferRunStateV2"
PAID_PLAN_SCHEMA = "PlannedPaidExecutionReceiptV2"
PREFLIGHT_AUTHORITY_SCHEMA = "SupervisedTransferPreflightAuthorityV2"
CONTRACT_CANARY_AUTHORITY_SCHEMA = "ReflectorContractCanaryAuthorityV2"
FROZEN_RECEIPT_SCHEMA = "FrozenThreeTargetTransferReceiptV2"
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}\Z", re.ASCII)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_FORMAL_CALLS = 10350
_FORMAL_MAXIMUM_CALLS = 14850
_PREFLIGHT_TASK_CALLS = 73
_PREFLIGHT_REFLECTOR_CALLS = 28
_CONTRACT_CANARY_TASK_CALLS = 8
_CONTRACT_CANARY_REFLECTOR_CALLS = 6
_CONTRACT_CANARY_CATEGORIES = ("Temperature_Prediction", "Name_Conversion")
_TASK_INFRASTRUCTURE_RETRY_LIMIT = 60
_TASK_INFRASTRUCTURE_RETRY_SECONDS = 60
_FINAL_TEST_INFRASTRUCTURE_RETRY_LIMIT = 60
_FINAL_TEST_INFRASTRUCTURE_RETRY_SECONDS = 60
_EXECUTOR_STALL_SECONDS = 20 * 60

_TRANSITIONS: dict[str, frozenset[str]] = {
    "INITIALIZED": frozenset(
        {"REFLECTOR_CONTRACT_CANARY", "UPDATE_SMOKE", "CONTROL_TRAIN"}
    ),
    "REFLECTOR_CONTRACT_CANARY": frozenset({"CONTRACT_CANARY_COMPLETED"}),
    "UPDATE_SMOKE": frozenset({"CONTROL_CANARY"}),
    "CONTROL_CANARY": frozenset({"ONLINE_CANARY"}),
    "ONLINE_CANARY": frozenset({"PREFLIGHT_COMPLETED"}),
    "CONTROL_TRAIN": frozenset({"ONLINE_CORE_INITIALIZATION"}),
    "ONLINE_CORE_INITIALIZATION": frozenset({"ONLINE_TRAIN"}),
    "ONLINE_TRAIN": frozenset({"ONLINE_TRAIN", "FREEZE_THREE_TARGETS"}),
    "FREEZE_THREE_TARGETS": frozenset({"FINAL_TEST_CONTROL"}),
    "FINAL_TEST_CONTROL": frozenset({"FINAL_TEST_EVOLVED"}),
    "FINAL_TEST_EVOLVED": frozenset({"REPORTING"}),
    "REPORTING": frozenset({"COMPLETED"}),
}


class SupervisedExperimentV2Error(RuntimeError):
    """Content-free terminal failure."""

    def __init__(self, finding_code: str) -> None:
        if re.fullmatch(r"[A-Z][A-Z0-9_]{2,95}", finding_code) is None:
            raise ValueError("invalid experiment finding code")
        self.finding_code = finding_code
        super().__init__(finding_code)


def _require_executor_retry_window(started_at: float) -> None:
    elapsed = max(0.0, time.monotonic() - started_at)
    if elapsed >= _EXECUTOR_STALL_SECONDS:
        raise SupervisedExperimentV2Error("EXECUTOR_STALLED")


def _executor_retry_delay_seconds(started_at: float, retry_seconds: int) -> float:
    elapsed = max(0.0, time.monotonic() - started_at)
    if elapsed >= _EXECUTOR_STALL_SECONDS:
        raise SupervisedExperimentV2Error("EXECUTOR_STALLED")
    remaining = _EXECUTOR_STALL_SECONDS - elapsed
    return min(float(retry_seconds), remaining)


class TaskExecutorV2(Protocol):
    def execute(self, request: SupervisedAgentRequestV2) -> RawAttempt: ...

    def consume_context_receipt(self, session_id: str) -> object: ...


@dataclass(frozen=True, slots=True)
class ExperimentInputsV2:
    repository_root: Path
    config: SupervisedTransferConfigV2
    loader: ChemBench4KDatasetLoader
    train: tuple[PrivateChemBench4KTask, ...]
    test: tuple[PrivateChemBench4KTask, ...]
    manifest_root: Path
    split_receipt_sha256: str
    split_summary_sha256: str
    source_manifest_sha256: str
    source_commit: str
    managed_codex: OpenEvoManagedCodexIdentityV2 | None
    candidate_codex: OpenEvoManagedCandidateCodexIdentityV2 | None
    runtime_services: OpenEvoRuntimeServicesIdentityV2 | None


@dataclass(frozen=True, slots=True)
class SessionOutcomeV2:
    evaluation: PrivateChemBench4KEvaluation
    attempt: RawAttempt
    session_id: str
    context_binding_sha256: str


@dataclass(frozen=True, slots=True)
class FrozenThreeTargetSetV2:
    contexts: dict[str, CoreResolvedSupervisedContextV2]
    artifact_set_digest: str
    receipt_sha256: str


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def load_experiment_inputs_v2(
    repository_root: Path,
    config: SupervisedTransferConfigV2,
    *,
    require_reflector_runtime: bool,
) -> ExperimentInputsV2:
    """Load the frozen split and optionally attest the reflector Codex binary."""

    if not isinstance(repository_root, Path) or not repository_root.is_absolute():
        raise TypeError("repository root must be absolute")
    repository = repository_root.resolve(strict=True)
    phase0 = verify_phase0_v2(repository)
    if phase0["status"] != "PASS" or phase0["test_actual_exposure_count"] != 0:
        raise SupervisedExperimentV2Error("SPLIT_ISOLATION_INVALID")
    snapshot = (repository / config.payload["dataset"]["root"]).resolve(strict=True)
    loader = ChemBench4KDatasetLoader(
        snapshot_root=snapshot,
        manifest_path=snapshot / "chembench4k_dataset_manifest_v2.json",
    )
    manifests = (repository / MANIFEST_ROOT).resolve(strict=True)
    split_receipt = manifests / "split_isolation_receipt_v2.json"
    split_summary = manifests / "split_summary.json"
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
    _require_partition_identity(train, test)
    if _git(repository, "diff", "--", "src/openevo"):
        raise SupervisedExperimentV2Error("SRC_OPENEVO_NOT_PRISTINE")
    source_manifest = verify_source_manifest_v2(repository)
    managed = (
        load_managed_codex_v2(repository_root=repository) if require_reflector_runtime else None
    )
    candidate = (
        load_managed_candidate_codex_v2(repository_root=repository)
        if require_reflector_runtime
        else None
    )
    runtime_services = (
        load_runtime_services_v2(repository_root=repository)
        if require_reflector_runtime
        else None
    )
    return ExperimentInputsV2(
        repository_root=repository,
        config=config,
        loader=loader,
        train=train,
        test=test,
        manifest_root=manifests,
        split_receipt_sha256=sha256_bytes(split_receipt.read_bytes()),
        split_summary_sha256=sha256_bytes(split_summary.read_bytes()),
        source_manifest_sha256=str(source_manifest["manifest_sha256"]),
        source_commit=_git(repository, "rev-parse", "HEAD"),
        managed_codex=managed,
        candidate_codex=candidate,
        runtime_services=runtime_services,
    )


def build_complete_dry_run_v2(inputs: ExperimentInputsV2) -> dict[str, object]:
    """Compile every prompt and transition without task or reflector calls."""

    prompts: dict[str, str] = {}
    demos: dict[str, tuple[str, ...]] = {}
    for task in (*inputs.train, *inputs.test):
        rendered = _render_prompt(inputs.loader, task)
        prompts[task.uid] = sha256_bytes(rendered.text.encode())
        demos[task.category] = rendered.demonstration_uids
    if len(prompts) != TRAIN_COUNT + TEST_COUNT:
        raise SupervisedExperimentV2Error("DRY_RUN_PROMPT_COUNT_INVALID")
    train_by_category = _by_category(inputs.train)
    if any(len(values) != 50 for values in train_by_category.values()):
        raise SupervisedExperimentV2Error("DRY_RUN_TRAIN_SCHEDULE_INVALID")
    schedule = [
        {
            "category": category,
            "task_count": len(train_by_category[category]),
            "sessions_per_task": TRAIN_ROUNDS,
            "cycles_per_task": EVOLUTION_CYCLES,
            "targets_per_cycle": list(TARGETS),
        }
        for category in CHEMBENCH4K_CATEGORIES
    ]
    budget = inputs.config.call_budget
    if (
        budget["total_model_calls"] != _FORMAL_CALLS
        or budget["maximum_model_calls"] != _FORMAL_MAXIMUM_CALLS
    ):
        raise SupervisedExperimentV2Error("DRY_RUN_CALL_BUDGET_INVALID")
    return {
        "schema_version": DRY_RUN_SCHEMA,
        "protocol_id": PROTOCOL_ID,
        "status": "PASS",
        "model_calls": 0,
        "planned_paid_calls": budget,
        "preflight_calls": {
            "task": _PREFLIGHT_TASK_CALLS,
            "reflector": _PREFLIGHT_REFLECTOR_CALLS,
            "core_jobs": _PREFLIGHT_REFLECTOR_CALLS * len(TARGETS),
        },
        "formal_stage_order": [
            "CONTROL_TRAIN",
            "ONLINE_TRAIN",
            "FREEZE_THREE_TARGETS",
            "FINAL_TEST_CONTROL",
            "FINAL_TEST_EVOLVED",
            "REPORTING",
        ],
        "train_schedule": schedule,
        "train_order_sha256": _uid_order_digest(inputs.train),
        "test_order_sha256": _uid_order_digest(inputs.test),
        "prompt_binding_sha256": sha256_bytes(canonical_json_bytes(prompts)),
        "demonstration_binding_sha256": sha256_bytes(
            canonical_json_bytes({key: list(value) for key, value in demos.items()})
        ),
        "split_receipt_sha256": inputs.split_receipt_sha256,
        "split_summary_sha256": inputs.split_summary_sha256,
        "source_manifest_sha256": inputs.source_manifest_sha256,
        "source_commit": inputs.source_commit,
        "candidate_execution_path": "TaskRequest->Rollout->Gateway->CodexHarness",
        "reflector_execution_path": "Core planned job->worker->codex_cli provider",
        "reflector_call_strategy": "one call per cycle, three independent typed artifacts",
        "test_evolution_jobs": 0,
        "probe_present": False,
    }


class SupervisedTransferExperimentV2:
    """Single-use preflight or formal controller with immutable run roots."""

    def __init__(
        self,
        *,
        inputs: ExperimentInputsV2,
        run_id: str,
        run_mode: Literal["contract_canary", "preflight", "formal"],
        preflight_run_id: str | None = None,
        contract_canary_run_id: str | None = None,
        readiness_receipt_sha256: str | None = None,
        executor_factory: Callable[[Literal["control", "online"]], TaskExecutorV2] | None = None,
        bridge_factory: Callable[[Path, str], TaskwiseCoreEvolutionBridgeV1] | None = None,
    ) -> None:
        if type(inputs) is not ExperimentInputsV2:
            raise TypeError("inputs must be exact ExperimentInputsV2")
        if type(run_id) is not str or _RUN_ID.fullmatch(run_id) is None:
            raise ValueError("invalid v2 run ID")
        if run_mode not in {"contract_canary", "preflight", "formal"}:
            raise ValueError("invalid run mode")
        if run_mode == "formal" and (
            type(preflight_run_id) is not str
            or _RUN_ID.fullmatch(preflight_run_id) is None
            or preflight_run_id == run_id
        ):
            raise ValueError("formal run requires a distinct preflight run")
        if run_mode == "formal" and contract_canary_run_id is None:
            raise ValueError("formal run requires a contract canary authority")
        if run_mode != "formal" and preflight_run_id is not None:
            raise ValueError("only formal can consume a preflight authority")
        if run_mode == "contract_canary" and contract_canary_run_id is not None:
            raise ValueError("contract canary cannot consume its own authority")
        if contract_canary_run_id is not None and (
            _RUN_ID.fullmatch(contract_canary_run_id) is None
            or contract_canary_run_id == run_id
        ):
            raise ValueError("invalid contract canary run ID")
        if readiness_receipt_sha256 is not None and (
            _SHA256.fullmatch(readiness_receipt_sha256) is None
        ):
            raise ValueError("invalid readiness receipt digest")
        if (
            inputs.managed_codex is None
            or inputs.candidate_codex is None
            or inputs.runtime_services is None
        ):
            raise SupervisedExperimentV2Error("REFLECTOR_RUNTIME_NOT_ATTESTED")
        if _benchmark_status(inputs.repository_root):
            raise SupervisedExperimentV2Error("BENCHMARK_SOURCE_NOT_COMMITTED")
        self.inputs = inputs
        self.run_id = run_id
        self.run_mode = run_mode
        self.preflight_run_id = preflight_run_id
        self.contract_canary_run_id = contract_canary_run_id
        self.readiness_receipt_sha256 = readiness_receipt_sha256
        self.result_root = (
            inputs.repository_root / inputs.config.payload["roots"]["results"] / "runs" / run_id
        )
        self.state_root = (
            inputs.repository_root / inputs.config.payload["roots"]["state"] / "runs" / run_id
        )
        if self.result_root.exists() or self.state_root.exists():
            raise SupervisedExperimentV2Error("RUN_ID_ALREADY_EXISTS")
        self.result_root.mkdir(parents=True, mode=0o755)
        self.state_root.mkdir(parents=True, mode=0o700)
        self.state_root.chmod(0o700)
        (self.result_root / "public").mkdir(mode=0o755)
        (self.state_root / "private").mkdir(mode=0o700)
        self.public_events = self.result_root / "public/events.jsonl"
        self.private_events = self.state_root / "private/events.jsonl"
        _exclusive_create(self.public_events, 0o644)
        _exclusive_create(self.private_events, 0o600)
        self._state: dict[str, object] = {
            "schema_version": RUN_STATE_SCHEMA,
            "protocol_id": PROTOCOL_ID,
            "run_id": run_id,
            "run_mode": run_mode,
            "preflight_run_id": preflight_run_id,
            "contract_canary_run_id": contract_canary_run_id,
            "readiness_receipt_sha256": readiness_receipt_sha256,
            "status": "INITIALIZED",
            "stage": "INITIALIZED",
            "source_commit": inputs.source_commit,
            "config_sha256": inputs.config.digest,
            "split_receipt_sha256": inputs.split_receipt_sha256,
            "split_summary_sha256": inputs.split_summary_sha256,
            "source_manifest_sha256": inputs.source_manifest_sha256,
            "reflector_codex_identity_sha256": inputs.managed_codex.digest,
            "reflector_codex_executable_sha256": inputs.managed_codex.executable_sha256,
            "candidate_codex_identity_sha256": inputs.candidate_codex.digest,
            "candidate_codex_executable_sha256": inputs.candidate_codex.executable_sha256,
            "runtime_services_identity_sha256": inputs.runtime_services.digest,
            "runtime_services_run_id": inputs.runtime_services.service_run_id,
            "managed_runtime_image": MANAGED_RUNTIME_RELEASES["managed_science"].loaded_image_id,
            "task_sessions": 0,
            "reflector_calls": 0,
            "core_jobs": 0,
            "context_resolutions": 0,
            "text_memory_artifacts": 0,
            "skill_artifacts": 0,
            "agent_system_artifacts": 0,
            "security_findings": 0,
            "context_findings": 0,
            "artifact_findings": 0,
            "infrastructure_failures": 0,
            "created_at_utc": utc_now(),
            "last_progress_utc": utc_now(),
        }
        self._write_state()
        self._evaluator = ChemBench4KPrivateEvaluator()
        self._heads: dict[str, TaskwiseCoreUpdateResultV1 | None] = {
            category: None for category in CHEMBENCH4K_CATEGORIES
        }
        self._bridges: dict[str, TaskwiseCoreEvolutionBridgeV1] = {}
        self._executor_factory = executor_factory or self._default_executor
        self._bridge_factory = bridge_factory or self._default_bridge
        self._write_paid_plan()

    def run_contract_canary(self) -> dict[str, object]:
        if self.run_mode != "contract_canary":
            raise SupervisedExperimentV2Error("RUN_MODE_MISMATCH")
        try:
            with ExitStack() as stack:
                online = _enter_if_context(stack, self._executor_factory("online"))
                self._set_stage("REFLECTOR_CONTRACT_CANARY")
                by_category = _by_category(self.inputs.train)
                for category in _CONTRACT_CANARY_CATEGORIES:
                    bridge = stack.enter_context(
                        self._bridge_factory(
                            self.state_root / "private/core/contract_canary" / category,
                            category,
                        )
                    )
                    self._run_one_online_task(
                        online,
                        task=by_category[category][0],
                        category_ordinal=0,
                        bridge=bridge,
                        stage="REFLECTOR_CONTRACT_CANARY",
                        persist_head=False,
                    )
            if (
                self._state["task_sessions"] != _CONTRACT_CANARY_TASK_CALLS
                or self._state["reflector_calls"] != _CONTRACT_CANARY_REFLECTOR_CALLS
                or self._state["core_jobs"] != _CONTRACT_CANARY_REFLECTOR_CALLS * 3
                or self._state["context_resolutions"]
                != _CONTRACT_CANARY_REFLECTOR_CALLS * 3
                or self._state["text_memory_artifacts"]
                != _CONTRACT_CANARY_REFLECTOR_CALLS
                or self._state["skill_artifacts"] != _CONTRACT_CANARY_REFLECTOR_CALLS
                or self._state["agent_system_artifacts"]
                != _CONTRACT_CANARY_REFLECTOR_CALLS
                or self._state["security_findings"] != 0
                or self._state["context_findings"] != 0
                or self._state["artifact_findings"] != 0
            ):
                raise SupervisedExperimentV2Error("CONTRACT_CANARY_COUNTER_MISMATCH")
            self._set_stage("CONTRACT_CANARY_COMPLETED")
            authority = self._contract_canary_authority()
            path = self.result_root / "public/contract_canary_authority_v2.json"
            write_public_file(path, canonical_pretty_json_bytes(authority))
            self._state["contract_canary_authority_sha256"] = sha256_bytes(path.read_bytes())
            self._state["status"] = "CONTRACT_CANARY_COMPLETED"
            self._state["completed_at_utc"] = utc_now()
            self._write_state()
            return authority
        except BaseException as exc:
            self._fail_closed(exc)
            raise

    def run_preflight(self) -> dict[str, object]:
        if self.run_mode != "preflight":
            raise SupervisedExperimentV2Error("RUN_MODE_MISMATCH")
        try:
            if self.contract_canary_run_id is not None:
                self._verify_contract_canary_authority()
            with ExitStack() as stack:
                control = _enter_if_context(stack, self._executor_factory("control"))
                online = _enter_if_context(stack, self._executor_factory("online"))
                self._run_update_smoke(stack, online)
                self._run_control_canary(control)
                self._run_online_canary(stack, online)
            if (
                self._state["task_sessions"] != _PREFLIGHT_TASK_CALLS
                or self._state["reflector_calls"] != _PREFLIGHT_REFLECTOR_CALLS
                or self._state["core_jobs"] != _PREFLIGHT_REFLECTOR_CALLS * 3
                or self._state["context_resolutions"] != _PREFLIGHT_REFLECTOR_CALLS * 3
                or self._state["text_memory_artifacts"] != _PREFLIGHT_REFLECTOR_CALLS
                or self._state["skill_artifacts"] != _PREFLIGHT_REFLECTOR_CALLS
                or self._state["agent_system_artifacts"] != _PREFLIGHT_REFLECTOR_CALLS
                or self._state["security_findings"] != 0
                or self._state["context_findings"] != 0
                or self._state["artifact_findings"] != 0
            ):
                raise SupervisedExperimentV2Error("PREFLIGHT_COUNTER_MISMATCH")
            self._set_stage("PREFLIGHT_COMPLETED")
            authority = self._preflight_authority()
            path = self.result_root / "public/preflight_authority_v2.json"
            write_public_file(path, canonical_pretty_json_bytes(authority))
            digest = sha256_bytes(path.read_bytes())
            self._state["preflight_authority_sha256"] = digest
            self._state["status"] = "PREFLIGHT_COMPLETED"
            self._state["completed_at_utc"] = utc_now()
            self._write_state()
            return {"status": "PREFLIGHT_COMPLETED", "run_id": self.run_id, **authority}
        except BaseException as exc:
            self._fail_closed(exc)
            raise

    def run_formal(self) -> dict[str, object]:
        if self.run_mode != "formal" or self.preflight_run_id is None:
            raise SupervisedExperimentV2Error("RUN_MODE_MISMATCH")
        try:
            self._verify_contract_canary_authority()
            self._verify_preflight_authority()
            with ExitStack() as stack:
                control = _enter_if_context(stack, self._executor_factory("control"))
                online = _enter_if_context(stack, self._executor_factory("online"))
                self._run_control_train(control)
                self._open_formal_bridges(stack)
                self._run_online_train(online)
                frozen = self._freeze_three_targets()
                self._run_final_test(frozen, control, online)
        except BaseException as exc:
            self._fail_closed(exc)
            raise

        self._set_stage("REPORTING")
        self._state["status"] = "EXPERIMENT_COMPLETED_REPORTING"
        self._state["test_completed_at_utc"] = utc_now()
        self._write_state()
        try:
            from openevo_chembench.supervised_transfer_v2.reporting import (
                build_reports_v2,
                export_final_html_to_windows_desktop_v2,
            )

            report = build_reports_v2(
                repository_root=self.inputs.repository_root,
                result_root=self.result_root,
                state_root=self.state_root,
                run_id=self.run_id,
            )
            desktop = export_final_html_to_windows_desktop_v2(
                report_html=Path(str(report["report_root"])) / "final_report.html",
                source_commit=self.inputs.source_commit,
                utc_token=datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
            )
            report["desktop_report"] = desktop
            self._set_stage("COMPLETED")
            self._state["status"] = "COMPLETED"
            self._state["completed_at_utc"] = utc_now()
            self._state["final_report_sha256"] = report["final_report_sha256"]
            self._state["desktop_report_sha256"] = desktop["sha256"]
            self._write_state()
            return report
        except BaseException as exc:
            self._state["status"] = "REPORTING_FAILED"
            self._state["reporting_failure_code"] = _failure_code(exc)
            self._state["reporting_failed_at_utc"] = utc_now()
            self._state["last_progress_utc"] = utc_now()
            self._write_state()
            self._append_public(
                {
                    "schema_version": "SupervisedTransferReportingFailurePublicV2",
                    "kind": "REPORTING_FAILED",
                    "stage": "REPORTING",
                    "failure_code": self._state["reporting_failure_code"],
                    "test_results_remain_frozen": True,
                    "failed_at_utc": self._state["reporting_failed_at_utc"],
                }
            )
            raise

    def _run_update_smoke(self, stack: ExitStack, executor: TaskExecutorV2) -> None:
        self._set_stage("UPDATE_SMOKE")
        task = _by_category(self.inputs.train)[CHEMBENCH4K_CATEGORIES[0]][0]
        bridge = stack.enter_context(
            self._bridge_factory(self.state_root / "private/core/update_smoke", task.category)
        )
        outcome = self._execute_session(
            executor,
            task=task,
            context=None,
            logical_arm="online_smoke",
            executor_arm="online",
            task_ordinal=0,
            round_index=0,
            stage="UPDATE_SMOKE",
        )
        trajectory = self._trajectory(task, 0, 0, outcome)
        result = bridge.apply_update(
            self._update_request(
                task=task,
                category_ordinal=0,
                round_index=0,
                trajectories=(trajectory,),
                evaluations=(outcome.evaluation,),
                predecessor=None,
                predecessor_context=None,
            )
        )
        bridge.issue_runtime_context(result)
        self._record_core_result("UPDATE_SMOKE", task.category, result)

    def _run_control_canary(self, executor: TaskExecutorV2) -> None:
        self._set_stage("CONTROL_CANARY")
        for ordinal, category in enumerate(CHEMBENCH4K_CATEGORIES):
            task = _by_category(self.inputs.train)[category][0]
            for round_index in range(TRAIN_ROUNDS):
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

    def _run_online_canary(self, stack: ExitStack, executor: TaskExecutorV2) -> None:
        self._set_stage("ONLINE_CANARY")
        for category in CHEMBENCH4K_CATEGORIES:
            task = _by_category(self.inputs.train)[category][0]
            bridge = stack.enter_context(
                self._bridge_factory(
                    self.state_root / "private/core/online_canary" / category,
                    category,
                )
            )
            self._run_one_online_task(
                executor,
                task=task,
                category_ordinal=0,
                bridge=bridge,
                stage="ONLINE_CANARY",
                persist_head=False,
            )

    def _run_control_train(self, executor: TaskExecutorV2) -> None:
        self._set_stage("CONTROL_TRAIN")
        for ordinal, task in enumerate(self.inputs.train):
            for round_index in range(TRAIN_ROUNDS):
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

    def _open_formal_bridges(self, stack: ExitStack) -> None:
        self._set_stage("ONLINE_CORE_INITIALIZATION")
        for category in CHEMBENCH4K_CATEGORIES:
            path = self.state_root / "private/core/formal" / category
            bridge = self._bridge_factory(path, category)
            self._bridges[category] = stack.enter_context(bridge)

    def _run_online_train(self, executor: TaskExecutorV2) -> None:
        self._set_stage("ONLINE_TRAIN")
        for category in CHEMBENCH4K_CATEGORIES:
            bridge = self._bridges[category]
            for category_ordinal, task in enumerate(_by_category(self.inputs.train)[category]):
                self._run_one_online_task(
                    executor,
                    task=task,
                    category_ordinal=category_ordinal,
                    bridge=bridge,
                    stage="ONLINE_TRAIN",
                    persist_head=True,
                )

    def _run_one_online_task(
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
        trajectories: list[SupervisedTrajectoryV2] = []
        evaluations: list[PrivateChemBench4KEvaluation] = []
        context = None if head is None else bridge.issue_runtime_context(head)
        for round_index in range(TRAIN_ROUNDS):
            outcome = self._execute_session(
                executor,
                task=task,
                context=context,
                logical_arm=("online_train" if stage == "ONLINE_TRAIN" else "online_canary"),
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
            request = self._update_request(
                task=task,
                category_ordinal=category_ordinal,
                round_index=round_index,
                trajectories=tuple(trajectories),
                evaluations=tuple(evaluations),
                predecessor=head,
                predecessor_context=context,
            )
            head = bridge.apply_update(request)
            context = bridge.issue_runtime_context(head)
            self._record_core_result(stage, task.category, head)
        if head is None or head.update_index != 3:
            raise SupervisedExperimentV2Error("ONLINE_TASK_FINAL_HEAD_MISSING")
        if persist_head:
            self._heads[task.category] = head
        return head

    def _update_request(
        self,
        *,
        task: PrivateChemBench4KTask,
        category_ordinal: int,
        round_index: Literal[0, 1, 2],
        trajectories: tuple[SupervisedTrajectoryV2, ...],
        evaluations: tuple[PrivateChemBench4KEvaluation, ...],
        predecessor: TaskwiseCoreUpdateResultV1 | None,
        predecessor_context: CoreResolvedSupervisedContextV2 | None,
    ) -> TaskwiseCoreUpdateRequestV1:
        packet = SupervisedEvolutionPacketV2.from_evaluations(
            task=task,
            training_task_ordinal=category_ordinal + 1,
            round_index=round_index,
            evaluations=evaluations,
            trajectory_ids=tuple(item.trajectory_id for item in trajectories),
            predecessor_memory=(
                None if predecessor_context is None else predecessor_context.memory.markdown
            ),
            predecessor_artifact_id=(
                None
                if predecessor_context is None
                else predecessor_context.memory.core_artifact_id
            ),
            predecessor_skill=(
                None if predecessor_context is None else predecessor_context.skill.markdown
            ),
            predecessor_skill_artifact_id=(
                None if predecessor_context is None else predecessor_context.skill.core_artifact_id
            ),
            predecessor_agent_system=(
                None if predecessor_context is None else predecessor_context.agent_system.markdown
            ),
            predecessor_agent_system_artifact_id=(
                None
                if predecessor_context is None
                else predecessor_context.agent_system.core_artifact_id
            ),
        )
        forbidden = tuple(
            dict.fromkeys(
                literal
                for trajectory in trajectories
                for literal in _trajectory_forbidden_literals(trajectory)
            )
        )
        return TaskwiseCoreUpdateRequestV1(
            task_uid=task.uid,
            task_index=category_ordinal,
            round_index=round_index,
            update_index=round_index + 1,
            trajectories=trajectories,
            predecessor=(None if predecessor is None else predecessor.predecessor_identity()),
            validator_forbidden_literals=forbidden,
            supervised_packet=packet,
        )

    def _trajectory(
        self,
        task: PrivateChemBench4KTask,
        category_ordinal: int,
        round_index: Literal[0, 1, 2],
        outcome: SessionOutcomeV2,
    ) -> SupervisedTrajectoryV2:
        return SupervisedTrajectoryV2.from_attempt(
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

    def _freeze_three_targets(self) -> FrozenThreeTargetSetV2:
        self._set_stage("FREEZE_THREE_TARGETS")
        contexts: dict[str, CoreResolvedSupervisedContextV2] = {}
        categories: dict[str, dict[str, object]] = {}
        for category in CHEMBENCH4K_CATEGORIES:
            head = self._heads[category]
            if head is None or head.global_update_ordinal != 150:
                raise SupervisedExperimentV2Error("FINAL_CATEGORY_HEAD_INCOMPLETE")
            bridge = self._bridges[category]
            bridge.verify_update_result(head)
            context = bridge.issue_runtime_context(head)
            contexts[category] = context
            binding = context.to_runtime_payload()
            categories[category] = {
                "global_update_ordinal": head.global_update_ordinal,
                "text_memory": _target_receipt(context.memory),
                "skill_bundle": _target_receipt(context.skill),
                "agent_system": _target_receipt(context.agent_system),
                "context_binding_sha256": sha256_bytes(canonical_json_bytes(binding)),
            }
        artifact_set_digest = sha256_bytes(canonical_json_bytes(categories))
        receipt = {
            "schema_version": FROZEN_RECEIPT_SCHEMA,
            "protocol_id": PROTOCOL_ID,
            "source_commit": self.inputs.source_commit,
            "config_sha256": self.inputs.config.digest,
            "split_receipt_sha256": self.inputs.split_receipt_sha256,
            "source_manifest_sha256": self.inputs.source_manifest_sha256,
            "test_order_sha256": _uid_order_digest(self.inputs.test),
            "category_count": len(categories),
            "target_count": len(categories) * len(TARGETS),
            "artifact_set_sha256": artifact_set_digest,
            "categories": categories,
            "test_evolution_allowed": False,
            "frozen_at_utc": utc_now(),
        }
        path = self.result_root / "public/frozen_three_target_transfer_receipt_v2.json"
        write_public_file(path, canonical_pretty_json_bytes(receipt))
        receipt_sha256 = sha256_bytes(path.read_bytes())
        self._state["frozen_artifact_set_sha256"] = artifact_set_digest
        self._state["frozen_receipt_sha256"] = receipt_sha256
        self._write_state()
        return FrozenThreeTargetSetV2(
            contexts=contexts,
            artifact_set_digest=artifact_set_digest,
            receipt_sha256=receipt_sha256,
        )

    def _run_final_test(
        self,
        frozen: FrozenThreeTargetSetV2,
        control: TaskExecutorV2,
        online: TaskExecutorV2,
    ) -> None:
        model_digest = sha256_bytes(
            canonical_json_bytes(
                {
                    "model": self.inputs.config.model,
                    "reasoning_effort": self.inputs.config.reasoning_effort,
                    "managed_runtime_image": MANAGED_RUNTIME_RELEASES[
                        "managed_science"
                    ].loaded_image_id,
                }
            )
        )
        ledger = FinalTestConsumptionLedgerV2(
            path=(
                self.inputs.repository_root
                / self.inputs.config.payload["roots"]["state"]
                / "private/final_test_consumption_ledger_v2.jsonl"
            ).resolve(),
            source_commit=self.inputs.source_commit,
            artifact_set_digest=frozen.artifact_set_digest,
            config_digest=self.inputs.config.digest,
            model_digest=model_digest,
        )
        for stage, arm, executor in (
            ("FINAL_TEST_CONTROL", "control", control),
            ("FINAL_TEST_EVOLVED", "evolved", online),
        ):
            self._set_stage(stage)
            for ordinal, task in enumerate(self.inputs.test):
                retry_window_started_at = time.monotonic()
                for attempt_number in range(1, _FINAL_TEST_INFRASTRUCTURE_RETRY_LIMIT + 1):
                    if attempt_number > 1:
                        _require_executor_retry_window(retry_window_started_at)
                    self._require_source_frozen()
                    context = None if arm == "control" else frozen.contexts[task.category]
                    attempt_id = self._session_id(stage, arm, task, 0, attempt_number)
                    ledger.claim(
                        task_uid=task.uid,
                        arm=arm,
                        attempt_id=attempt_id,
                        timestamp=utc_now(),
                    )
                    try:
                        self._execute_session(
                            executor,
                            task=task,
                            context=context,
                            logical_arm=f"test_{arm}",
                            executor_arm=("control" if arm == "control" else "online"),
                            task_ordinal=ordinal,
                            round_index=0,
                            stage=stage,
                            session_id_override=attempt_id,
                            completion_callback=lambda completion, *, _task=task, _arm=arm, _attempt_id=attempt_id: (
                                ledger.complete(
                                    task_uid=_task.uid,
                                    arm=_arm,
                                    attempt_id=_attempt_id,
                                    completion=completion,
                                    timestamp=utc_now(),
                                )
                            ),
                        )
                        break
                    except SupervisedTaskExecutionErrorV2 as exc:
                        if exc.completion_exists is True:
                            if exc.completion_sha256 is None:
                                raise SupervisedExperimentV2Error(
                                    "TEST_COMPLETION_DIGEST_MISSING"
                                ) from exc
                            ledger.complete_digest(
                                task_uid=task.uid,
                                arm=arm,
                                attempt_id=attempt_id,
                                completion_sha256=exc.completion_sha256,
                                timestamp=utc_now(),
                            )
                            raise
                        retryable = (
                            exc.code is SupervisedTaskExecutionCodeV2.TASK_FAILED
                            and exc.completion_exists is False
                            and attempt_number < _FINAL_TEST_INFRASTRUCTURE_RETRY_LIMIT
                        )
                        if not retryable:
                            raise
                        retry_after_seconds = _executor_retry_delay_seconds(
                            retry_window_started_at,
                            _FINAL_TEST_INFRASTRUCTURE_RETRY_SECONDS,
                        )
                        self._append_public(
                            {
                                "schema_version": "FinalTestInfrastructureRetryPublicV2",
                                "kind": "INFRASTRUCTURE_RETRY_NO_COMPLETION",
                                "stage": stage,
                                "arm": arm,
                                "task_uid": task.uid,
                                "attempt_number": attempt_number,
                                "same_source_config_artifact_model": True,
                                "completion_exists": False,
                                "retry_after_seconds": retry_after_seconds,
                                "stall_budget_seconds": _EXECUTOR_STALL_SECONDS,
                                "recorded_at_utc": utc_now(),
                            }
                        )
                        time.sleep(retry_after_seconds)
                else:
                    raise SupervisedExperimentV2Error("FINAL_TEST_INFRASTRUCTURE_RETRY_EXHAUSTED")
        summary = ledger.summary()
        if (
            summary["control_completion_count"] != TEST_COUNT
            or summary["evolved_completion_count"] != TEST_COUNT
        ):
            raise SupervisedExperimentV2Error("FINAL_TEST_LEDGER_INCOMPLETE")
        write_public_file(
            self.result_root / "public/final_test_ledger_receipt_v2.json",
            canonical_pretty_json_bytes(summary),
        )

    def _execute_session(
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
        session_id_override: str | None = None,
        completion_callback: Callable[[str], object] | None = None,
    ) -> SessionOutcomeV2:
        if session_id_override is not None:
            return self._execute_single_session(
                executor,
                task=task,
                context=context,
                logical_arm=logical_arm,
                executor_arm=executor_arm,
                task_ordinal=task_ordinal,
                round_index=round_index,
                stage=stage,
                session_id=session_id_override,
                completion_callback=completion_callback,
            )
        retry_window_started_at = time.monotonic()
        for attempt_number in range(1, _TASK_INFRASTRUCTURE_RETRY_LIMIT + 1):
            if attempt_number > 1:
                _require_executor_retry_window(retry_window_started_at)
            session_id = self._session_id(
                stage,
                logical_arm,
                task,
                round_index,
                attempt_number,
            )
            try:
                return self._execute_single_session(
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
            except SupervisedTaskExecutionErrorV2 as exc:
                retryable = (
                    exc.code is SupervisedTaskExecutionCodeV2.TASK_FAILED
                    and exc.completion_exists is False
                    and attempt_number < _TASK_INFRASTRUCTURE_RETRY_LIMIT
                )
                if not retryable:
                    raise
                retry_after_seconds = _executor_retry_delay_seconds(
                    retry_window_started_at,
                    _TASK_INFRASTRUCTURE_RETRY_SECONDS,
                )
                self._append_public(
                    {
                        "schema_version": "TaskInfrastructureRetryPublicV2",
                        "kind": "INFRASTRUCTURE_RETRY_NO_COMPLETION",
                        "stage": stage,
                        "logical_arm": logical_arm,
                        "task_uid": task.uid,
                        "round_index": round_index,
                        "attempt_number": attempt_number,
                        "same_input_and_system_state": True,
                        "completion_exists": False,
                        "retry_after_seconds": retry_after_seconds,
                        "stall_budget_seconds": _EXECUTOR_STALL_SECONDS,
                        "recorded_at_utc": utc_now(),
                    }
                )
                time.sleep(retry_after_seconds)
        raise SupervisedExperimentV2Error("TASK_INFRASTRUCTURE_RETRY_EXHAUSTED")

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
    ) -> SessionOutcomeV2:
        if self._state["stage"] != stage:
            raise SupervisedExperimentV2Error("SESSION_STAGE_NOT_ADMITTED")
        if executor_arm == "control" and context is not None:
            raise SupervisedExperimentV2Error("CONTROL_CONTEXT_FORBIDDEN")
        if stage == "FINAL_TEST_EVOLVED" and (executor_arm != "online" or context is None):
            raise SupervisedExperimentV2Error("EVOLVED_TEST_CONTEXT_REQUIRED")
        if executor_arm == "online" and stage in {
            "ONLINE_TRAIN",
            "ONLINE_CANARY",
            "REFLECTOR_CONTRACT_CANARY",
        }:
            expected_missing = task_ordinal == 0 and round_index == 0
            if (context is None) != expected_missing:
                raise SupervisedExperimentV2Error("ONLINE_CONTEXT_SEQUENCE_INVALID")
        prompt = _render_prompt(self.inputs.loader, task)
        self._append_private(
            {
                "schema_version": "SupervisedTaskAttemptPrivateV2",
                "kind": "TASK_MODEL_ATTEMPTED",
                "stage": stage,
                "logical_arm": logical_arm,
                "task_uid": task.uid,
                "category": task.category,
                "task_ordinal": task_ordinal,
                "round_index": round_index,
                "session_id": session_id,
                "prompt_sha256": sha256_bytes(prompt.text.encode()),
                "started_at_utc": utc_now(),
            }
        )
        try:
            attempt = executor.execute(
                SupervisedAgentRequestV2(
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
            if completion_callback is not None:
                completion_callback(attempt.response)
            receipt = executor.consume_context_receipt(session_id)
            require_match = getattr(receipt, "require_match", None)
            digest = getattr(receipt, "digest", None)
            if not callable(require_match) or type(digest) is not str:
                raise SupervisedExperimentV2Error("CONTEXT_RECEIPT_INVALID")
            require_match()
            evaluation = self._evaluator.evaluate(
                task=task,
                raw_completion=attempt.response,
            )
        except BaseException:
            self._state["infrastructure_failures"] = (
                int(self._state["infrastructure_failures"]) + 1
            )
            self._write_state()
            raise
        public = {
            "schema_version": "SupervisedTaskCompletionPublicV2",
            "kind": "TASK_MODEL_EXECUTED",
            "stage": stage,
            "logical_arm": logical_arm,
            "task_uid": task.uid,
            "category": task.category,
            "task_ordinal": task_ordinal,
            "round_index": round_index,
            "session_id": session_id,
            "official_prediction": evaluation.official.prediction,
            "official_parse_status": evaluation.official.status.value,
            "strict_prediction": evaluation.strict.prediction,
            "strict_parse_status": evaluation.strict.status.value,
            "context_binding_sha256": digest,
            "context_artifact_ids": _context_artifact_ids(context),
            "completed_at_utc": utc_now(),
        }
        private = {
            **public,
            "schema_version": "SupervisedTaskEvaluationPrivateV2",
            "kind": "PRIVATE_EVALUATED",
            "target": task.target,
            "raw_completion": evaluation.raw_completion,
            "correct": evaluation.correct,
            "transcript_reference": attempt.transcript_reference.reference,
        }
        self._append_public(public)
        self._append_private(private)
        self._state["task_sessions"] = int(self._state["task_sessions"]) + 1
        self._state["category"] = task.category
        self._state["task"] = task_ordinal
        self._state["round_or_cycle"] = round_index
        self._state["last_progress_utc"] = utc_now()
        self._write_state()
        return SessionOutcomeV2(
            evaluation=evaluation,
            attempt=attempt,
            session_id=session_id,
            context_binding_sha256=digest,
        )

    def _record_core_result(
        self,
        stage: str,
        category: str,
        result: TaskwiseCoreUpdateResultV1,
    ) -> None:
        auxiliaries = {value.target_id: value for value in result.supervised_auxiliary_artifacts}
        if set(auxiliaries) != {"skill_bundle", "agent_system"}:
            raise SupervisedExperimentV2Error("THREE_TARGET_RESULT_INCOMPLETE")
        self._append_public(
            {
                "schema_version": "SupervisedCoreCyclePublicV2",
                "kind": "REFLECTOR_SUPERVISED",
                "stage": stage,
                "category": category,
                "task_uid": result.task_uid,
                "task_index": result.task_index,
                "cycle": result.update_index,
                "global_update_ordinal": result.global_update_ordinal,
                "packet_sha256": result.supervised_packet_sha256,
                "dataset_artifact_id": result.dataset_artifact_id,
                "reflector_job_id": result.job_id,
                "text_memory_artifact_id": result.core_artifact_id,
                "skill_artifact_id": auxiliaries["skill_bundle"].core_artifact_id,
                "agent_system_artifact_id": auxiliaries["agent_system"].core_artifact_id,
                "text_memory_payload_sha256": result.artifact_payload_sha256,
                "skill_payload_sha256": auxiliaries["skill_bundle"].artifact_payload_sha256,
                "agent_system_payload_sha256": auxiliaries["agent_system"].artifact_payload_sha256,
                "text_memory_bytes": result.memory_inspection.utf8_byte_count,
                "skill_bytes": auxiliaries["skill_bundle"].inspection.utf8_byte_count,
                "agent_system_bytes": auxiliaries["agent_system"].inspection.utf8_byte_count,
                "confirmed_rules": result.memory_inspection.confirmed_rule_count,
                "provisional_rules": result.memory_inspection.provisional_rule_count,
                "retired_rules": result.memory_inspection.retired_rule_count,
                "failure_modes": result.memory_inspection.failure_mode_count,
                "skill_section_item_counts": list(
                    auxiliaries["skill_bundle"].inspection.section_item_counts
                ),
                "agent_system_section_item_counts": list(
                    auxiliaries["agent_system"].inspection.section_item_counts
                ),
                "completed_at_utc": utc_now(),
            }
        )
        self._state["reflector_calls"] = int(self._state["reflector_calls"]) + 1
        self._state["core_jobs"] = int(self._state["core_jobs"]) + 3
        self._state["context_resolutions"] = (
            int(self._state["context_resolutions"]) + 3
        )
        for key in (
            "text_memory_artifacts",
            "skill_artifacts",
            "agent_system_artifacts",
        ):
            self._state[key] = int(self._state[key]) + 1
        self._state["round_or_cycle"] = result.update_index
        self._state["last_progress_utc"] = utc_now()
        self._write_state()

    def _default_executor(self, arm: Literal["control", "online"]) -> TaskExecutorV2:
        return SupervisedManagedCodexExecutorV2(
            arm=arm,
            timeout_seconds=self.inputs.config.task_timeout_seconds,
            rollout_url=self.inputs.config.rollout_url,
            runtime_services=self.inputs.runtime_services,
        )

    def _default_bridge(
        self,
        state_root: Path,
        _category: str,
    ) -> TaskwiseCoreEvolutionBridgeV1:
        managed = self.inputs.managed_codex
        if managed is None:
            raise SupervisedExperimentV2Error("REFLECTOR_RUNTIME_NOT_ATTESTED")
        return build_supervised_core_bridge_at_roots_v2(
            state_root=state_root.resolve(),
            framework_lock=(
                self.inputs.repository_root / self.inputs.config.framework_lock
            ).resolve(strict=True),
            codex_executable=managed.executable,
            auth_source=codex_subscription_auth_source_v2(),
            timeout_seconds=self.inputs.config.reflector_timeout_seconds,
        )

    def _write_paid_plan(self) -> None:
        if self.run_mode == "contract_canary":
            calls = {
                "candidate_task_calls": _CONTRACT_CANARY_TASK_CALLS,
                "candidate_subscription_readiness_calls_minimum": (
                    _CONTRACT_CANARY_TASK_CALLS
                ),
                "candidate_subscription_readiness_calls_maximum": (
                    _CONTRACT_CANARY_TASK_CALLS * 2
                ),
                "reflector_calls": _CONTRACT_CANARY_REFLECTOR_CALLS,
                "answer_and_reflector_model_calls": (
                    _CONTRACT_CANARY_TASK_CALLS + _CONTRACT_CANARY_REFLECTOR_CALLS
                ),
                "total_model_calls": (
                    _CONTRACT_CANARY_TASK_CALLS * 2 + _CONTRACT_CANARY_REFLECTOR_CALLS
                ),
                "maximum_model_calls": (
                    _CONTRACT_CANARY_TASK_CALLS * 3 + _CONTRACT_CANARY_REFLECTOR_CALLS
                ),
            }
        elif self.run_mode == "preflight":
            calls = {
                "candidate_task_calls": _PREFLIGHT_TASK_CALLS,
                "candidate_subscription_readiness_calls_minimum": (
                    _PREFLIGHT_TASK_CALLS
                ),
                "candidate_subscription_readiness_calls_maximum": (
                    _PREFLIGHT_TASK_CALLS * 2
                ),
                "reflector_calls": _PREFLIGHT_REFLECTOR_CALLS,
                "answer_and_reflector_model_calls": (
                    _PREFLIGHT_TASK_CALLS + _PREFLIGHT_REFLECTOR_CALLS
                ),
                "total_model_calls": (
                    _PREFLIGHT_TASK_CALLS * 2 + _PREFLIGHT_REFLECTOR_CALLS
                ),
                "maximum_model_calls": (
                    _PREFLIGHT_TASK_CALLS * 3 + _PREFLIGHT_REFLECTOR_CALLS
                ),
            }
        else:
            calls = self.inputs.config.call_budget
        payload = {
            "schema_version": PAID_PLAN_SCHEMA,
            "protocol_id": PROTOCOL_ID,
            "run_id": self.run_id,
            "run_mode": self.run_mode,
            "contract_canary_run_id": self.contract_canary_run_id,
            "readiness_receipt_sha256": self.readiness_receipt_sha256,
            "source_commit": self.inputs.source_commit,
            "config_sha256": self.inputs.config.digest,
            "split_receipt_sha256": self.inputs.split_receipt_sha256,
            "source_manifest_sha256": self.inputs.source_manifest_sha256,
            "model": self.inputs.config.model,
            "reasoning_effort": self.inputs.config.reasoning_effort,
            "task_timeout_seconds": self.inputs.config.task_timeout_seconds,
            "reflector_timeout_seconds": self.inputs.config.reflector_timeout_seconds,
            "executor_stall_seconds": _EXECUTOR_STALL_SECONDS,
            "task_infrastructure_retry_limit": _TASK_INFRASTRUCTURE_RETRY_LIMIT,
            "task_infrastructure_retry_seconds": _TASK_INFRASTRUCTURE_RETRY_SECONDS,
            "final_test_infrastructure_retry_limit": (
                _FINAL_TEST_INFRASTRUCTURE_RETRY_LIMIT
            ),
            "final_test_infrastructure_retry_seconds": (
                _FINAL_TEST_INFRASTRUCTURE_RETRY_SECONDS
            ),
            "task_execution_policy": self.inputs.config.payload["executor"],
            "reflector_execution_policy": self.inputs.config.payload["reflector"],
            "task_max_output_tokens": None,
            "task_max_output_tokens_reason": (
                "Codex subscription CLI has no supported output-token cap; the benchmark "
                "records both official and exact-one-letter parse outcomes"
            ),
            "reflector_max_output_tokens": None,
            "reflector_max_last_message_bytes": 65536,
            "reflector_strategy": "one_structured_call_three_typed_artifacts",
            "candidate_readiness_call_accounting": (
                "Every immutable candidate session runs one real Core-managed subscription "
                "readiness codex exec before the answer call; one additional readiness call "
                "is reserved only for the Core refusal-retry branch."
            ),
            "candidate_codex_identity_sha256": self.inputs.candidate_codex.digest,
            "candidate_codex_executable_sha256": (self.inputs.candidate_codex.executable_sha256),
            "reflector_codex_identity_sha256": self.inputs.managed_codex.digest,
            "reflector_codex_executable_sha256": (self.inputs.managed_codex.executable_sha256),
            "runtime_services_identity_sha256": self.inputs.runtime_services.digest,
            "runtime_services_run_id": self.inputs.runtime_services.service_run_id,
            "calls": calls,
            "written_before_first_model_call": True,
            "created_at_utc": utc_now(),
        }
        write_public_file(
            self.result_root / "public/planned_paid_execution_receipt_v2.json",
            canonical_pretty_json_bytes(payload),
        )

    def _contract_canary_authority(self) -> dict[str, object]:
        return {
            "schema_version": CONTRACT_CANARY_AUTHORITY_SCHEMA,
            "protocol_id": PROTOCOL_ID,
            "status": "PASS",
            "source_run_id": self.run_id,
            "source_commit": self.inputs.source_commit,
            "config_sha256": self.inputs.config.digest,
            "split_receipt_sha256": self.inputs.split_receipt_sha256,
            "source_manifest_sha256": self.inputs.source_manifest_sha256,
            "readiness_receipt_sha256": self.readiness_receipt_sha256,
            "reflector_codex_identity_sha256": self.inputs.managed_codex.digest,
            "candidate_codex_identity_sha256": self.inputs.candidate_codex.digest,
            "runtime_services_identity_sha256": self.inputs.runtime_services.digest,
            "runtime_services_run_id": self.inputs.runtime_services.service_run_id,
            "categories": list(_CONTRACT_CANARY_CATEGORIES),
            "task_sessions": self._state["task_sessions"],
            "reflector_calls": self._state["reflector_calls"],
            "core_jobs": self._state["core_jobs"],
            "context_resolutions": self._state["context_resolutions"],
            "text_memory_artifacts": self._state["text_memory_artifacts"],
            "skill_artifacts": self._state["skill_artifacts"],
            "agent_system_artifacts": self._state["agent_system_artifacts"],
            "security_findings": self._state["security_findings"],
            "context_findings": self._state["context_findings"],
            "artifact_findings": self._state["artifact_findings"],
            "test_model_calls": 0,
            "artifacts_reusable": False,
            "issued_at_utc": utc_now(),
        }

    def _verify_contract_canary_authority(self) -> None:
        if self.contract_canary_run_id is None:
            raise SupervisedExperimentV2Error("CONTRACT_CANARY_AUTHORITY_MISSING")
        path = (
            self.inputs.repository_root
            / self.inputs.config.payload["roots"]["results"]
            / "runs"
            / self.contract_canary_run_id
            / "public/contract_canary_authority_v2.json"
        )
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise SupervisedExperimentV2Error("CONTRACT_CANARY_AUTHORITY_MISSING") from exc
        expected = {
            "schema_version": CONTRACT_CANARY_AUTHORITY_SCHEMA,
            "protocol_id": PROTOCOL_ID,
            "status": "PASS",
            "source_run_id": self.contract_canary_run_id,
            "source_commit": self.inputs.source_commit,
            "config_sha256": self.inputs.config.digest,
            "split_receipt_sha256": self.inputs.split_receipt_sha256,
            "source_manifest_sha256": self.inputs.source_manifest_sha256,
            "readiness_receipt_sha256": self.readiness_receipt_sha256,
            "reflector_codex_identity_sha256": self.inputs.managed_codex.digest,
            "candidate_codex_identity_sha256": self.inputs.candidate_codex.digest,
            "runtime_services_identity_sha256": self.inputs.runtime_services.digest,
            "runtime_services_run_id": self.inputs.runtime_services.service_run_id,
            "categories": list(_CONTRACT_CANARY_CATEGORIES),
            "task_sessions": _CONTRACT_CANARY_TASK_CALLS,
            "reflector_calls": _CONTRACT_CANARY_REFLECTOR_CALLS,
            "core_jobs": _CONTRACT_CANARY_REFLECTOR_CALLS * 3,
            "context_resolutions": _CONTRACT_CANARY_REFLECTOR_CALLS * 3,
            "text_memory_artifacts": _CONTRACT_CANARY_REFLECTOR_CALLS,
            "skill_artifacts": _CONTRACT_CANARY_REFLECTOR_CALLS,
            "agent_system_artifacts": _CONTRACT_CANARY_REFLECTOR_CALLS,
            "security_findings": 0,
            "context_findings": 0,
            "artifact_findings": 0,
            "test_model_calls": 0,
            "artifacts_reusable": False,
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            raise SupervisedExperimentV2Error("CONTRACT_CANARY_AUTHORITY_INVALID")
        self._state["contract_canary_authority_sha256"] = sha256_bytes(path.read_bytes())
        self._write_state()

    def _preflight_authority(self) -> dict[str, object]:
        return {
            "schema_version": PREFLIGHT_AUTHORITY_SCHEMA,
            "protocol_id": PROTOCOL_ID,
            "status": "PASS",
            "source_run_id": self.run_id,
            "source_commit": self.inputs.source_commit,
            "config_sha256": self.inputs.config.digest,
            "split_receipt_sha256": self.inputs.split_receipt_sha256,
            "split_summary_sha256": self.inputs.split_summary_sha256,
            "source_manifest_sha256": self.inputs.source_manifest_sha256,
            "contract_canary_run_id": self.contract_canary_run_id,
            "contract_canary_authority_sha256": self._state.get(
                "contract_canary_authority_sha256"
            ),
            "readiness_receipt_sha256": self.readiness_receipt_sha256,
            "reflector_codex_identity_sha256": self.inputs.managed_codex.digest,
            "candidate_codex_identity_sha256": self.inputs.candidate_codex.digest,
            "runtime_services_identity_sha256": self.inputs.runtime_services.digest,
            "runtime_services_run_id": self.inputs.runtime_services.service_run_id,
            "task_sessions": self._state["task_sessions"],
            "reflector_calls": self._state["reflector_calls"],
            "core_jobs": self._state["core_jobs"],
            "context_resolutions": self._state["context_resolutions"],
            "text_memory_artifacts": self._state["text_memory_artifacts"],
            "skill_artifacts": self._state["skill_artifacts"],
            "agent_system_artifacts": self._state["agent_system_artifacts"],
            "security_findings": self._state["security_findings"],
            "context_findings": self._state["context_findings"],
            "artifact_findings": self._state["artifact_findings"],
            "test_model_calls": 0,
            "issued_at_utc": utc_now(),
        }

    def _verify_preflight_authority(self) -> None:
        assert self.preflight_run_id is not None
        path = (
            self.inputs.repository_root
            / self.inputs.config.payload["roots"]["results"]
            / "runs"
            / self.preflight_run_id
            / "public/preflight_authority_v2.json"
        )
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise SupervisedExperimentV2Error("PREFLIGHT_AUTHORITY_MISSING") from exc
        expected = {
            "schema_version": PREFLIGHT_AUTHORITY_SCHEMA,
            "protocol_id": PROTOCOL_ID,
            "status": "PASS",
            "source_run_id": self.preflight_run_id,
            "source_commit": self.inputs.source_commit,
            "config_sha256": self.inputs.config.digest,
            "split_receipt_sha256": self.inputs.split_receipt_sha256,
            "split_summary_sha256": self.inputs.split_summary_sha256,
            "source_manifest_sha256": self.inputs.source_manifest_sha256,
            "contract_canary_run_id": self.contract_canary_run_id,
            "contract_canary_authority_sha256": self._state.get(
                "contract_canary_authority_sha256"
            ),
            "readiness_receipt_sha256": self.readiness_receipt_sha256,
            "reflector_codex_identity_sha256": self.inputs.managed_codex.digest,
            "candidate_codex_identity_sha256": self.inputs.candidate_codex.digest,
            "runtime_services_identity_sha256": self.inputs.runtime_services.digest,
            "runtime_services_run_id": self.inputs.runtime_services.service_run_id,
            "task_sessions": _PREFLIGHT_TASK_CALLS,
            "reflector_calls": _PREFLIGHT_REFLECTOR_CALLS,
            "core_jobs": _PREFLIGHT_REFLECTOR_CALLS * 3,
            "context_resolutions": _PREFLIGHT_REFLECTOR_CALLS * 3,
            "text_memory_artifacts": _PREFLIGHT_REFLECTOR_CALLS,
            "skill_artifacts": _PREFLIGHT_REFLECTOR_CALLS,
            "agent_system_artifacts": _PREFLIGHT_REFLECTOR_CALLS,
            "security_findings": 0,
            "context_findings": 0,
            "artifact_findings": 0,
            "test_model_calls": 0,
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            raise SupervisedExperimentV2Error("PREFLIGHT_AUTHORITY_INVALID")
        self._state["preflight_authority_sha256"] = sha256_bytes(path.read_bytes())
        self._write_state()

    def _require_source_frozen(self) -> None:
        if (
            _git(self.inputs.repository_root, "rev-parse", "HEAD") != self.inputs.source_commit
            or _benchmark_status(self.inputs.repository_root)
            or _git(self.inputs.repository_root, "diff", "--", "src/openevo")
        ):
            raise SupervisedExperimentV2Error("TEST_SOURCE_IDENTITY_DRIFT")
        source_manifest = (
            self.inputs.repository_root
            / "benchmarks/chembench/manifests/supervised_transfer_v2/source_manifest_v2.json"
        )
        if sha256_bytes(source_manifest.read_bytes()) != self.inputs.source_manifest_sha256:
            raise SupervisedExperimentV2Error("TEST_SOURCE_MANIFEST_DRIFT")
        frozen = self.result_root / "public/frozen_three_target_transfer_receipt_v2.json"
        if sha256_bytes(frozen.read_bytes()) != self._state.get("frozen_receipt_sha256"):
            raise SupervisedExperimentV2Error("TEST_ARTIFACT_SET_DRIFT")

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
        return f"stv2-{stage.lower().replace('_', '-')[:18]}-{digest[:32]}"

    def _set_stage(self, stage: str) -> None:
        current = str(self._state["stage"])
        if stage != current and stage not in _TRANSITIONS.get(current, frozenset()):
            raise SupervisedExperimentV2Error("STAGE_TRANSITION_INVALID")
        self._state["stage"] = stage
        self._state["status"] = "RUNNING" if stage != "COMPLETED" else "COMPLETED"
        self._state["last_progress_utc"] = utc_now()
        self._write_state()

    def _fail_closed(self, exc: BaseException) -> None:
        self._state["status"] = "FAIL_CLOSED"
        self._state["failure_code"] = _failure_code(exc)
        self._state["failed_at_utc"] = utc_now()
        self._state["last_progress_utc"] = utc_now()
        self._write_state()
        self._append_public(
            {
                "schema_version": "SupervisedTransferFailurePublicV2",
                "kind": "FAIL_CLOSED",
                "stage": self._state["stage"],
                "failure_code": self._state["failure_code"],
                "failed_at_utc": self._state["failed_at_utc"],
            }
        )

    def _append_public(self, payload: dict[str, object]) -> None:
        _append_jsonl(self.public_events, payload, mode=0o644)

    def _append_private(self, payload: dict[str, object]) -> None:
        _append_jsonl(self.private_events, payload, mode=0o600)

    def _write_state(self) -> None:
        write_private_file(
            self.state_root / "run_state.json",
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


def _by_category(
    tasks: Iterable[PrivateChemBench4KTask],
) -> dict[str, tuple[PrivateChemBench4KTask, ...]]:
    values = tuple(tasks)
    return {
        category: tuple(task for task in values if task.category == category)
        for category in CHEMBENCH4K_CATEGORIES
    }


def _require_partition_identity(
    train: tuple[PrivateChemBench4KTask, ...],
    test: tuple[PrivateChemBench4KTask, ...],
) -> None:
    if len(train) != TRAIN_COUNT or len(test) != TEST_COUNT:
        raise SupervisedExperimentV2Error("SPLIT_COUNT_INVALID")
    if {task.uid for task in train} & {task.uid for task in test}:
        raise SupervisedExperimentV2Error("SPLIT_INTERSECTION_INVALID")
    for values in (train, test):
        if {
            category: sum(task.category == category for task in values)
            for category in CHEMBENCH4K_CATEGORIES
        } != {category: 50 for category in CHEMBENCH4K_CATEGORIES}:
            raise SupervisedExperimentV2Error("SPLIT_BALANCE_INVALID")


def _context_artifact_ids(
    context: CoreResolvedSupervisedContextV2 | None,
) -> list[str]:
    if context is None:
        return []
    return [
        context.memory.core_artifact_id,
        context.skill.core_artifact_id,
        context.agent_system.core_artifact_id,
    ]


def _target_receipt(
    target: CoreResolvedTextMemoryV2 | CoreResolvedAuxiliaryArtifactV2,
) -> dict[str, str]:
    resolved_content_sha256 = (
        target.resolved_memory_sha256
        if type(target) is CoreResolvedTextMemoryV2
        else target.resolved_content_sha256
    )
    return {
        "artifact_id": target.core_artifact_id,
        "artifact_payload_sha256": target.artifact_payload_sha256,
        "resolved_content_sha256": resolved_content_sha256,
        "context_resolution_digest": target.context_resolution_digest,
    }


def _uid_order_digest(tasks: Iterable[PrivateChemBench4KTask]) -> str:
    return sha256_bytes(canonical_json_bytes([task.uid for task in tasks]))


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", "-C", os.fspath(repository), *arguments),
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _benchmark_status(repository: Path) -> str:
    return _git(
        repository,
        "status",
        "--short",
        "--untracked-files=all",
        "--",
        "benchmarks/chembench",
    )


def _exclusive_create(path: Path, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    os.close(descriptor)


def _append_jsonl(path: Path, payload: dict[str, object], *, mode: int) -> None:
    encoded = canonical_json_bytes(payload)
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND)
    with os.fdopen(descriptor, "ab", buffering=0) as stream:
        stream.write(encoded)
        os.fsync(stream.fileno())
    path.chmod(mode)


def _failure_code(exc: BaseException) -> str:
    for name in ("finding_code", "code"):
        value = getattr(exc, name, None)
        if isinstance(value, str):
            return value
        nested = getattr(value, "value", None)
        if isinstance(nested, str):
            return nested.upper()
    if isinstance(exc, SupervisedExperimentV2Error):
        return exc.finding_code
    return type(exc).__name__.upper()[:96]


def _enter_if_context(stack: ExitStack, value: TaskExecutorV2) -> TaskExecutorV2:
    enter = getattr(value, "__enter__", None)
    exit_method = getattr(value, "__exit__", None)
    if callable(enter) and callable(exit_method):
        return stack.enter_context(value)  # type: ignore[arg-type]
    return value


__all__ = [
    "DRY_RUN_SCHEMA",
    "ExperimentInputsV2",
    "FrozenThreeTargetSetV2",
    "SessionOutcomeV2",
    "SupervisedExperimentV2Error",
    "SupervisedTransferExperimentV2",
    "build_complete_dry_run_v2",
    "load_experiment_inputs_v2",
]
