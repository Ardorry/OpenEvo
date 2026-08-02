"""Formal, resumable Temperature full-evolve v1 experiment runner.

This is the single paid-run owner.  It consumes an already frozen zero-call
preflight and private split, uses the managed OpenEvo Rollout services for every
Candidate/Reflector call, uses the verified Core coordinator for every target
update, and derives restart position exclusively from private checkpoints plus
the fsync ledger.  Importing this module starts no service and invokes no model.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, NoReturn

from openevo.evolution.framework import load_verified_framework_registry
from openevo.evolution.store import EvolutionStore

from openevo_chembench.chembench4k_dataset import (
    ChemBench4KDatasetLoader,
    normalize_benchmark_text,
)
from openevo_chembench.chembench4k_models import PrivateChemBench4KTask
from openevo_chembench.chembench4k_prompt import render_official_five_shot_prompt
from openevo_chembench.supervised_transfer_v1.common import (
    canonical_json_bytes,
    canonical_pretty_json_bytes,
    sha256_bytes,
    write_private_file,
)
from openevo_chembench.supervised_transfer_v2.artifacts import (
    CoreResolvedSupervisedContextV2,
)
from openevo_chembench.supervised_transfer_v2.context_binding import (
    SupervisedSessionContextBindingV2,
)
from openevo_chembench.temperature_full_evolve_v1.artifacts import (
    ProjectedArtifactSetV1,
    project_artifacts,
)
from openevo_chembench.temperature_full_evolve_v1.candidate import (
    CandidateCallPlanV1,
    EvaluatedCandidateV1,
    TemperatureCandidateError,
    prepare_candidate_call_v1,
)
from openevo_chembench.temperature_full_evolve_v1.config import (
    BATCH_SIZE,
    PROTOCOL_ID,
    TEST_COUNT,
    TRAIN_COUNT,
    TemperatureFullEvolveConfigV1,
    load_temperature_full_evolve_config,
)
from openevo_chembench.temperature_full_evolve_v1.controller import (
    TemperatureExperimentControllerV1,
    execute_and_commit_core_batch_v1,
)
from openevo_chembench.temperature_full_evolve_v1.core_evolution import (
    CoreEvolutionStateV1,
    CoreTextMemoryDatasetBindingV1,
    TemperatureCoreEvolutionCoordinatorV1,
    TemperatureCoreEvolutionError,
    render_core_text_memory_dataset_v1,
)
from openevo_chembench.temperature_full_evolve_v1.evidence import RuleEvidenceIndexV1
from openevo_chembench.temperature_full_evolve_v1.execution import (
    FormalCallEnvelopeV1,
    TemperatureFormalExecutorV1,
    finalize_candidate_outcome_v1,
    finalize_reflector_outcome_v1,
    recover_existing_candidate_plan_v1,
    recover_existing_reflector_plan_v1,
)
from openevo_chembench.temperature_full_evolve_v1.formal_runtime import (
    TemperatureFormalRuntimeError,
    load_temperature_formal_runtime_v1,
)
from openevo_chembench.temperature_full_evolve_v1.ledger import (
    REFLECTOR_ATTEMPT_RETRY_EXHAUSTED,
    TemperatureExperimentLedgerV1,
)
from openevo_chembench.temperature_full_evolve_v1.packet import (
    BatchAggregateDiagnosticV1,
    BatchTrainCaseV1,
    ReflectorArtifactSnapshotV1,
    build_batch_supervised_packet_v1,
    seal_batch_supervised_packet_v1,
)
from openevo_chembench.temperature_full_evolve_v1.preflight import (
    RegressionReceiptV1,
    RuntimePreflightEvidenceV1,
    build_zero_model_preflight_v1,
)
from openevo_chembench.temperature_full_evolve_v1.reflector import (
    AcceptedReflectorSynthesisV1,
    TemperatureReflectorError,
    is_retryable_reflector_completion_rejection_v1,
    prepare_reflector_call_v1,
)
from openevo_chembench.temperature_full_evolve_v1.reporting import (
    AggregateReportInputV1,
)
from openevo_chembench.temperature_full_evolve_v1.runtime_services import (
    TemperatureRuntimeServicesIdentityV1,
    audit_empty_temperature_runtime_services_v1,
    load_temperature_runtime_services_v1,
)
from openevo_chembench.temperature_full_evolve_v1.split import (
    FrozenTemperatureSplitV1,
    build_temperature_split_v1,
    render_temperature_split_artifacts_v1,
)
from openevo_chembench.temperature_full_evolve_v1.statistics import (
    paired_binary_metrics_v1,
)

RUN_MANIFEST_SCHEMA = "TemperatureFullEvolveFormalRunManifestV1"
ARM_MANIFEST_SCHEMA = "TemperatureFullEvolveArmManifestV1"
RUNNER_CHECKPOINT_SCHEMA = "TemperatureFullEvolveRunnerCheckpointV1"
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,110}\Z", re.ASCII)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_COMMIT = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_CALL_SUFFIX = re.compile(r"-i(?P<ordinal>[0-9]{3})\Z", re.ASCII)


class TemperatureFormalRunnerError(RuntimeError):
    """Content-free runner admission, checkpoint, or orchestration finding."""

    def __init__(self, finding_code: str) -> None:
        self.finding_code = finding_code
        super().__init__(finding_code)


@dataclass(frozen=True, slots=True)
class TemperatureRunLayoutV1:
    repository_root: Path
    controller_run_id: str
    run_root: Path
    private_root: Path
    status_path: Path
    ledger_path: Path
    controller_manifest_path: Path
    core_root: Path
    core_checkpoint_path: Path
    evolved_run_id: str
    evolved_root: Path
    baseline_run_id: str
    baseline_root: Path
    core_run_id: str

    @classmethod
    def build(
        cls,
        *,
        repository_root: Path,
        controller_run_id: str,
    ) -> TemperatureRunLayoutV1:
        repository = repository_root.resolve(strict=True)
        if _RUN_ID.fullmatch(controller_run_id) is None:
            raise ValueError("controller run ID is invalid")
        state = repository / "state/chembench_temperature_full_evolve_v1/runs"
        run_root = state / controller_run_id
        private = run_root / "private"
        evolved_run_id = f"{controller_run_id}-evolved-test"
        baseline_run_id = f"{controller_run_id}-baseline-test"
        core_run_id = f"{controller_run_id}-core"
        if any(
            _RUN_ID.fullmatch(value) is None
            for value in (evolved_run_id, baseline_run_id, core_run_id)
        ):
            raise ValueError("derived arm run ID is invalid")
        return cls(
            repository_root=repository,
            controller_run_id=controller_run_id,
            run_root=run_root,
            private_root=private,
            status_path=run_root / "status/current.json",
            ledger_path=private / "events.jsonl",
            controller_manifest_path=private / "run_manifest_v1.json",
            core_root=run_root / "core",
            core_checkpoint_path=private / "core_coordinator_checkpoint_v1.json",
            evolved_run_id=evolved_run_id,
            evolved_root=run_root / "arms/evolved",
            baseline_run_id=baseline_run_id,
            baseline_root=run_root / "arms/baseline",
            core_run_id=core_run_id,
        )

    @property
    def all_run_ids(self) -> tuple[str, ...]:
        return (
            self.controller_run_id,
            self.evolved_run_id,
            self.baseline_run_id,
            self.core_run_id,
        )


@dataclass(frozen=True, slots=True)
class FormalAdmissionEvidenceV1:
    config: TemperatureFullEvolveConfigV1
    split: FrozenTemperatureSplitV1
    preflight_root: Path
    preflight_report: dict[str, Any]
    split_manifest: dict[str, Any]
    historical_manifest: dict[str, Any]
    grouping_manifest: dict[str, Any]
    config_manifest: dict[str, Any]
    model_identity_receipt: dict[str, Any]
    runtime_identity_receipt: dict[str, Any]
    regression_receipt: RegressionReceiptV1
    preflight_bundle_sha256: str
    source_commit: str
    branch: str


class TemperatureFullEvolveFormalRunnerV1:
    """Run or recover the complete fixed 100/100 formal experiment."""

    def __init__(
        self,
        *,
        repository_root: Path,
        controller_run_id: str,
        preflight_root: Path,
        private_split_root: Path,
        resume: bool,
    ) -> None:
        self.layout = TemperatureRunLayoutV1.build(
            repository_root=repository_root,
            controller_run_id=controller_run_id,
        )
        self._resume = resume
        self._recovery_recorded = False
        verified_preflight_root, verified_private_split_root = _validated_admission_roots(
            repository=self.layout.repository_root,
            preflight_root=preflight_root,
            private_split_root=private_split_root,
        )
        self._private_split_root = verified_private_split_root
        self._runtime = load_temperature_runtime_services_v1(
            repository_root=self.layout.repository_root
        )
        self._admission = _load_and_verify_admission(
            repository=self.layout.repository_root,
            preflight_root=verified_preflight_root,
            private_split_root=self._private_split_root,
            runtime=self._runtime,
        )
        self._started_at_utc: str
        self._initialize_or_recover_layout()
        self._ledger = TemperatureExperimentLedgerV1(
            path=self.layout.ledger_path,
            run_id=self.layout.controller_run_id,
        )
        self._controller = TemperatureExperimentControllerV1(
            run_id=self.layout.controller_run_id,
            ledger=self._ledger,
            status_path=self.layout.status_path,
        )
        self._executor = TemperatureFormalExecutorV1(
            runtime_services=self._runtime,
            ledger=self._ledger,
        )
        registry = load_verified_framework_registry(
            self.layout.repository_root / self._admission.config.framework_lock_relative
        )
        self._core_store = EvolutionStore(
            db_path=self.layout.core_root / "evolution.sqlite3",
            artifact_root=self.layout.core_root / "artifacts",
            executable_registry=registry,
        )
        self._core_store.initialize()
        forbidden_questions = tuple(task.question for task in self._admission.split.train)
        if self.layout.core_checkpoint_path.exists():
            self._core = TemperatureCoreEvolutionCoordinatorV1.recover_from_private_checkpoint_bytes(
                _read_private_bytes(self.layout.core_checkpoint_path),
                executable_registry=registry,
                planned_job_port=self._core_store,
                core_artifact_root=self.layout.core_root / "artifacts",
                forbidden_questions=forbidden_questions,
            )
        else:
            self._core = TemperatureCoreEvolutionCoordinatorV1(
                executable_registry=registry,
                planned_job_port=self._core_store,
                core_artifact_root=self.layout.core_root / "artifacts",
            )
        self._forbidden_questions = forbidden_questions
        self._dev = tuple(
            sorted(
                ChemBench4KDatasetLoader(
                    snapshot_root=self.layout.repository_root
                    / str(self._admission.config.payload["dataset"]["root"])
                ).load_category("Temperature_Prediction", split="dev"),
                key=lambda task: task.source_index,
            )
        )
        _validate_dev_demonstrations(self._admission.split, self._dev)

    def close(self) -> None:
        ledger = getattr(self, "_ledger", None)
        if ledger is not None:
            ledger.close()

    def run(self) -> Path:
        """Continue until audit closure or a fail-closed exception."""

        try:
            self._ensure_runtime_inventory_admission()
            self._ensure_core_state_checkpoint_chain()
            self._ensure_run_and_split_events()
            if any(event["kind"] == "RUN_FAILED" for event in self._ledger.events):
                raise TemperatureFormalRunnerError(
                    REFLECTOR_ATTEMPT_RETRY_EXHAUSTED
                )
            diagnostics: list[BatchAggregateDiagnosticV1] = self._load_closed_diagnostics()
            self._record_successful_resume()
            for batch_index in range(1, 5):
                if _has_semantic_event(
                    self._ledger.events, "BATCH_POST_CLOSED", batch_index=batch_index
                ):
                    if len(diagnostics) < batch_index:
                        if self._core.head.batch_index != batch_index:
                            raise TemperatureFormalRunnerError(
                                "RUNNER_CLOSED_BATCH_METRICS_MISSING"
                            )
                        recovered_context = self._context_for_state(self._core.head)
                        if recovered_context is None:
                            raise TemperatureFormalRunnerError(
                                "RUNNER_CLOSED_BATCH_CONTEXT_MISSING"
                            )
                        recovered_evidence = self._load_evidence(batch_index)
                        recovered_diagnostic = _batch_diagnostic_from_ledger(
                            run_id=self.layout.controller_run_id,
                            batch_index=batch_index,
                            events=self._ledger.events,
                            artifact_total_utf8_bytes=(
                                self._core.head.total_artifact_utf8_bytes
                            ),
                        )
                        self._write_batch_checkpoints(
                            batch_index=batch_index,
                            diagnostic=recovered_diagnostic,
                            evidence=recovered_evidence,
                            state=self._core.head,
                            context=recovered_context,
                        )
                        diagnostics.append(recovered_diagnostic)
                    else:
                        self._load_evidence(batch_index)
                        self._load_batch_report(batch_index)
                    continue
                if len(diagnostics) not in {batch_index - 1, batch_index}:
                    raise TemperatureFormalRunnerError("RUNNER_BATCH_METRICS_SEQUENCE_INVALID")
                batch_tasks = self._admission.split.train[
                    (batch_index - 1) * BATCH_SIZE : batch_index * BATCH_SIZE
                ]
                committed = _has_semantic_event(
                    self._ledger.events,
                    "BATCH_ARTIFACT_SET_COMMITTED",
                    batch_index=batch_index,
                )
                if not committed or self._core.head.batch_index < batch_index:
                    if self._core.head.batch_index != batch_index - 1:
                        raise TemperatureFormalRunnerError("RUNNER_CORE_PREDECESSOR_INVALID")
                    prior_evidence = self._load_evidence(batch_index - 1)
                    prior_context = self._context_for_state(self._core.head)
                    pre = self._run_candidate_phase(
                        tasks=batch_tasks,
                        phase="train_pre",
                        batch_index=batch_index,
                        context=prior_context,
                        run_id=self.layout.controller_run_id,
                        ordinal_offset=(batch_index - 1) * BATCH_SIZE,
                    )
                    if not _has_semantic_event(
                        self._ledger.events, "BATCH_PRE_CLOSED", batch_index=batch_index
                    ):
                        self._controller.append_protocol_event(
                            "BATCH_PRE_CLOSED",
                            {
                                "batch_index": batch_index,
                                "accepted_count": BATCH_SIZE,
                                "evaluated_count": BATCH_SIZE,
                            },
                        )
                    packet = self._build_packet(
                        batch_index=batch_index,
                        tasks=batch_tasks,
                        pre=pre,
                        evidence=prior_evidence,
                        diagnostics=tuple(diagnostics[: batch_index - 1]),
                        context=prior_context,
                    )
                    sealed = seal_batch_supervised_packet_v1(
                        packet,
                        all_train_uids=frozenset(self._admission.split.plan.train_uids),
                        seen_train_uids=frozenset(
                            self._admission.split.plan.train_uids[
                                : batch_index * BATCH_SIZE
                            ]
                        ),
                        test_uids=frozenset(self._admission.split.plan.test_uids),
                    )
                    reflection = self._run_reflector(sealed)
                    evidence = reflection.next_evidence
                    if not _has_semantic_event(
                        self._ledger.events,
                        "EVIDENCE_VALIDATED",
                        batch_index=batch_index,
                    ):
                        self._controller.append_protocol_event(
                            "EVIDENCE_VALIDATED",
                            {
                                "batch_index": batch_index,
                                "evidence_sha256": evidence.digest,
                                "rule_count": len(evidence.rules),
                            },
                        )
                    _write_private_once(
                        self.layout.private_root / f"evidence/C{batch_index}.json",
                        canonical_json_bytes(evidence.model_dump(mode="json")),
                    )
                    projected = project_artifacts(
                        evidence,
                        forbidden_questions=self._forbidden_questions,
                    )
                    successor = self._run_core_batch(
                        batch_index=batch_index,
                        packet_sha256=packet.digest,
                        reflection=reflection,
                        evidence=evidence,
                        projected=projected,
                    )
                else:
                    if self._core.head.batch_index != batch_index:
                        raise TemperatureFormalRunnerError("RUNNER_CORE_STATE_AHEAD")
                    successor = self._core.head
                    evidence = self._load_evidence(batch_index)
                context = self._context_for_state(successor)
                if context is None:
                    raise TemperatureFormalRunnerError("RUNNER_SUCCESSOR_CONTEXT_MISSING")
                self._run_candidate_phase(
                    tasks=batch_tasks,
                    phase="train_post",
                    batch_index=batch_index,
                    context=context,
                    run_id=self.layout.controller_run_id,
                    ordinal_offset=(batch_index - 1) * BATCH_SIZE,
                )
                diagnostic = _batch_diagnostic_from_ledger(
                    run_id=self.layout.controller_run_id,
                    batch_index=batch_index,
                    events=self._ledger.events,
                    artifact_total_utf8_bytes=successor.total_artifact_utf8_bytes,
                )
                self._write_batch_checkpoints(
                    batch_index=batch_index,
                    diagnostic=diagnostic,
                    evidence=evidence,
                    state=successor,
                    context=context,
                )
                if not _has_semantic_event(
                    self._ledger.events, "BATCH_POST_CLOSED", batch_index=batch_index
                ):
                    self._controller.append_protocol_event(
                        "BATCH_POST_CLOSED",
                        {
                            "batch_index": batch_index,
                            "accepted_count": BATCH_SIZE,
                            "evaluated_count": BATCH_SIZE,
                        },
                    )
                if len(diagnostics) < batch_index:
                    diagnostics.append(diagnostic)
                elif diagnostics[batch_index - 1] != diagnostic:
                    raise TemperatureFormalRunnerError("RUNNER_BATCH_METRICS_DRIFT")

            final_state = self._core.head
            evidence = self._load_evidence(4)
            context = self._context_for_state(final_state)
            if final_state.batch_index != 4 or context is None:
                raise TemperatureFormalRunnerError("RUNNER_FINAL_C4_MISSING")
            if not _has_semantic_event(self._ledger.events, "FINAL_STATE_FROZEN"):
                frozen_context = _frozen_context_target_payload(context)
                self._controller.append_protocol_event(
                    "FINAL_STATE_FROZEN",
                    {
                        "batch_index": 4,
                        "state_sha256": final_state.digest,
                        "artifact_count": 3,
                        "feedback_disabled": True,
                        "test_sealed": True,
                        **frozen_context,
                    },
                )
            evolved = self._run_candidate_phase(
                tasks=self._admission.split.test,
                phase="evolved_test",
                batch_index=None,
                context=context,
                run_id=self.layout.evolved_run_id,
                ordinal_offset=0,
            )
            if not _has_semantic_event(self._ledger.events, "EVOLVED_TEST_CLOSED"):
                self._controller.append_protocol_event(
                    "EVOLVED_TEST_CLOSED",
                    _test_closure_payload(evolved, baseline=False),
                )
            baseline = self._run_candidate_phase(
                tasks=self._admission.split.test,
                phase="baseline_test",
                batch_index=None,
                context=None,
                run_id=self.layout.baseline_run_id,
                ordinal_offset=0,
            )
            if not _has_semantic_event(self._ledger.events, "BASELINE_TEST_CLOSED"):
                self._controller.append_protocol_event(
                    "BASELINE_TEST_CLOSED",
                    _test_closure_payload(baseline, baseline=True),
                )
            aggregate_path = self._close_audit_and_write_aggregate_input(
                diagnostics=tuple(diagnostics),
                evidence=evidence,
                final_state=final_state,
                context=context,
                evolved=evolved,
                baseline=baseline,
            )
            self._controller.write_status()
            return aggregate_path
        except BaseException:
            try:
                self._controller.write_status()
            except Exception as status_error:  # noqa: BLE001 - preserve original failure
                del status_error
            raise

    def _ensure_runtime_inventory_admission(self) -> None:
        path = self.layout.private_root / "empty_runtime_admission_receipt_v1.json"
        expected_digest = self._admission.runtime_identity_receipt.get(
            "empty_runtime_inventory_sha256"
        )
        if type(expected_digest) is not str or _SHA256.fullmatch(expected_digest) is None:
            raise TemperatureFormalRunnerError(
                "RUNNER_EMPTY_RUNTIME_PREFLIGHT_RECEIPT_INVALID"
            )
        if self._resume:
            receipt = _load_private_json(path)
        else:
            if any(event["kind"] == "CALL_CLAIMED" for event in self._ledger.events):
                raise TemperatureFormalRunnerError("RUNNER_FRESH_LEDGER_NOT_EMPTY")
            receipt = audit_empty_temperature_runtime_services_v1(
                repository_root=self.layout.repository_root
            )
            _write_private_once(path, canonical_json_bytes(receipt))
        if (
            receipt.get("inventory_sha256") != expected_digest
            or receipt.get("service_run_id") != self._runtime.service_run_id
            or receipt.get("runtime_services_identity_sha256") != self._runtime.digest
            or receipt.get("rollout_task_count") != 0
            or receipt.get("persisted_task_directory_count") != 0
            or receipt.get("model_calls_observed") != 0
        ):
            raise TemperatureFormalRunnerError("RUNNER_EMPTY_RUNTIME_RECEIPT_MISMATCH")

    def _ensure_run_and_split_events(self) -> None:
        if not _has_semantic_event(self._ledger.events, "RUN_CREATED"):
            self._controller.append_protocol_event(
                "RUN_CREATED",
                {
                    "generation_zero": True,
                    "old_artifact_imported": False,
                    "old_completion_imported": False,
                    "old_database_imported": False,
                },
            )
        if not _has_semantic_event(self._ledger.events, "SPLIT_FROZEN"):
            self._controller.append_protocol_event(
                "SPLIT_FROZEN",
                {
                    "split_sha256": self._admission.split.plan.split_sha256,
                    "train_count": TRAIN_COUNT,
                    "test_count": TEST_COUNT,
                    "batch_size": BATCH_SIZE,
                    "test_sealed": True,
                },
            )

    def _record_successful_resume(self) -> None:
        """Record one content-free recovery after all authoritative state checks pass."""

        if (
            not self._resume
            or self._recovery_recorded
            or _has_semantic_event(self._ledger.events, "AUDIT_CLOSED")
            or any(event["kind"] == "RUN_FAILED" for event in self._ledger.events)
        ):
            return
        checkpoint = self._core_state_checkpoint_path(self._core.head.batch_index)
        checkpoint_sha256 = _file_sha256(checkpoint)
        recovery_index = 1 + sum(
            event["kind"] == "RECOVERY_COMPLETED" for event in self._ledger.events
        )
        self._ledger.append(
            "RECOVERY_COMPLETED",
            {
                "recovery_index": recovery_index,
                "recovery_mode": "resume",
                "recovered_ledger_head_sha256": self._ledger.head_digest,
                "core_checkpoint_sha256": checkpoint_sha256,
                "core_batch_index": self._core.head.batch_index,
            },
        )
        self._recovery_recorded = True

    def _run_candidate_phase(
        self,
        *,
        tasks: tuple[PrivateChemBench4KTask, ...],
        phase: Literal["train_pre", "train_post", "evolved_test", "baseline_test"],
        batch_index: int | None,
        context: CoreResolvedSupervisedContextV2 | None,
        run_id: str,
        ordinal_offset: int,
    ) -> tuple[EvaluatedCandidateV1, ...]:
        completed: dict[int, EvaluatedCandidateV1] = {}
        while len(completed) < len(tasks):
            plans: list[CandidateCallPlanV1] = []
            task_by_logical: dict[str, PrivateChemBench4KTask] = {}
            for local_ordinal, task in enumerate(tasks):
                ordinal = ordinal_offset + local_ordinal
                logical = _candidate_logical_call_id(
                    run_id=run_id,
                    phase=phase,
                    batch_index=batch_index,
                    task_ordinal=ordinal,
                )
                if ordinal in completed:
                    continue
                prompt = render_official_five_shot_prompt(
                    task.to_public(),
                    category_dev=self._dev,
                )
                workspace = (
                    None
                    if context is None
                    else self._phase_workspace(
                        phase=phase,
                        batch_index=batch_index,
                        context=context,
                    )
                )
                latest = self._ledger.latest_claim(logical)
                if latest is not None and not self._ledger.failure_has_no_completion(
                    str(latest["call_id"])
                ):
                    envelope = self._load_call_checkpoint(str(latest["call_id"]))
                    plan = recover_existing_candidate_plan_v1(
                        envelope=envelope,
                        task=task,
                        prompt=prompt,
                        context=context,
                        context_workspace=workspace,
                        phase=phase,
                        task_ordinal=ordinal,
                        batch_index=batch_index,
                        ledger=self._ledger,
                        run_id=run_id,
                        service_identity_sha256=self._runtime.digest,
                    )
                else:
                    try:
                        plan = prepare_candidate_call_v1(
                            task=task,
                            prompt=prompt,
                            context=context,
                            context_workspace=workspace,
                            phase=phase,
                            task_ordinal=ordinal,
                            batch_index=batch_index,
                            ledger=self._ledger,
                            run_id=run_id,
                            service_identity_sha256=self._runtime.digest,
                        )
                    except TemperatureCandidateError as exc:
                        raise TemperatureFormalRunnerError(
                            "RUNNER_CANDIDATE_PLAN_FAILED"
                        ) from exc
                    self._write_call_checkpoint(FormalCallEnvelopeV1.from_candidate(plan))
                plans.append(plan)
                task_by_logical[plan.logical_call_id] = task
            if not plans:
                raise TemperatureFormalRunnerError("RUNNER_CANDIDATE_PHASE_STALLED")
            outcomes = self._executor.run_many_to_durable_terminal(
                tuple(FormalCallEnvelopeV1.from_candidate(plan) for plan in plans),
                max_workers=3,
            )
            for plan, outcome in zip(plans, outcomes, strict=True):
                if outcome.state == "terminal_no_completion":
                    continue
                evaluated = finalize_candidate_outcome_v1(
                    outcome=outcome,
                    plan=plan,
                    task=task_by_logical[plan.logical_call_id],
                    ledger=self._ledger,
                )
                completed[plan.task_ordinal] = evaluated
                self._controller.write_status()
        expected = tuple(range(ordinal_offset, ordinal_offset + len(tasks)))
        if tuple(sorted(completed)) != expected:
            raise TemperatureFormalRunnerError("RUNNER_CANDIDATE_ORDINAL_CLOSURE_INVALID")
        return tuple(completed[index] for index in expected)

    def _run_reflector(self, sealed: Any) -> AcceptedReflectorSynthesisV1:
        logical = _reflector_logical_call_id(
            self.layout.controller_run_id,
            sealed.packet.batch_index,
        )
        latest = self._ledger.latest_claim(logical)
        if self._ledger.accepted_call(logical) is not None:
            envelope = self._load_call_checkpoint(str(latest["call_id"]))
            plan = recover_existing_reflector_plan_v1(
                envelope=envelope,
                sealed=sealed,
                ledger=self._ledger,
                run_id=self.layout.controller_run_id,
                service_identity_sha256=self._runtime.digest,
            )
            outcome = self._executor.run_many_to_durable_terminal((envelope,), max_workers=1)[0]
            return finalize_reflector_outcome_v1(
                outcome=outcome,
                plan=plan,
                sealed=sealed,
                ledger=self._ledger,
            )
        while True:
            latest = self._ledger.latest_claim(logical)
            if latest is not None and not (
                self._ledger.failure_has_no_completion(str(latest["call_id"]))
                or self._ledger.completion_was_rejected(str(latest["call_id"]))
            ):
                envelope = self._load_call_checkpoint(str(latest["call_id"]))
                plan = recover_existing_reflector_plan_v1(
                    envelope=envelope,
                    sealed=sealed,
                    ledger=self._ledger,
                    run_id=self.layout.controller_run_id,
                    service_identity_sha256=self._runtime.digest,
                )
            else:
                try:
                    plan = prepare_reflector_call_v1(
                        sealed=sealed,
                        ledger=self._ledger,
                        run_id=self.layout.controller_run_id,
                        service_identity_sha256=self._runtime.digest,
                    )
                except TemperatureReflectorError as exc:
                    if exc.finding_code != REFLECTOR_ATTEMPT_RETRY_EXHAUSTED:
                        raise
                    self._terminate_reflector_retry_exhausted(
                        batch_index=sealed.packet.batch_index,
                    )
                envelope = FormalCallEnvelopeV1.from_reflector(plan)
                self._write_call_checkpoint(envelope)
            outcome = self._executor.run_many_to_durable_terminal((envelope,), max_workers=1)[0]
            if outcome.state == "terminal_no_completion":
                continue
            try:
                accepted = finalize_reflector_outcome_v1(
                    outcome=outcome,
                    plan=plan,
                    sealed=sealed,
                    ledger=self._ledger,
                )
            except TemperatureReflectorError as exc:
                if not (
                    is_retryable_reflector_completion_rejection_v1(exc)
                    and self._ledger.completion_was_rejected(plan.call_id)
                ):
                    raise
                self._controller.write_status()
                continue
            self._controller.write_status()
            return accepted

    def _terminate_reflector_retry_exhausted(self, *, batch_index: int) -> NoReturn:
        self._controller.record_reflector_retry_exhausted(batch_index=batch_index)
        raise TemperatureFormalRunnerError(REFLECTOR_ATTEMPT_RETRY_EXHAUSTED)

    def _run_core_batch(
        self,
        *,
        batch_index: int,
        packet_sha256: str,
        reflection: AcceptedReflectorSynthesisV1,
        evidence: RuleEvidenceIndexV1,
        projected: ProjectedArtifactSetV1,
    ) -> CoreEvolutionStateV1:
        if self._core.head.batch_index >= batch_index:
            if self._core.head.batch_index != batch_index:
                raise TemperatureFormalRunnerError("RUNNER_CORE_STATE_AHEAD")
            return self._core.head
        rendered = render_core_text_memory_dataset_v1(
            batch_index=batch_index,
            projected_memory=projected.memory,
            dataset_directory=(
                self.layout.core_root
                / "artifacts/input_datasets"
                / PROTOCOL_ID
                / f"batch_{batch_index}"
            ),
            source_packet_sha256=packet_sha256,
            reflector_receipt_sha256=sha256_bytes(
                canonical_json_bytes(reflection.reflector_accepted_payload)
            ),
            evidence_sha256=evidence.digest,
            projected_artifact_set_sha256=projected.digest,
        )
        dataset_evidence_root = (
            self.layout.private_root / f"core_dataset_bindings/batch_{batch_index}"
        )
        binding = _ensure_core_dataset_binding_v1(
            store=self._core_store,
            rendered=rendered,
            artifact_root=self.layout.core_root / "artifacts",
            intent_path=dataset_evidence_root / "registration_intent.json",
            binding_path=dataset_evidence_root / "binding.json",
        )
        prepared = self._core.prepare_batch(
            batch_index=batch_index,
            predecessor=self._core.head,
            source_packet_sha256=packet_sha256,
            reflector_receipt_sha256=sha256_bytes(
                canonical_json_bytes(reflection.reflector_accepted_payload)
            ),
            evidence=evidence,
            projected_artifacts=projected,
            text_memory_dataset=binding,
            forbidden_questions=self._forbidden_questions,
        )
        self._save_core_checkpoint()
        successor = execute_and_commit_core_batch_v1(
            controller=self._controller,
            coordinator=self._core,
            prepared=prepared,
            forbidden_questions=self._forbidden_questions,
        )
        self._save_core_checkpoint()
        self._write_core_state_checkpoint(successor)
        return successor

    def _context_for_state(
        self, state: CoreEvolutionStateV1
    ) -> CoreResolvedSupervisedContextV2 | None:
        if state.batch_index == 0:
            return None
        if state.digest != self._core.head.digest:
            raise TemperatureFormalRunnerError("RUNNER_NON_HEAD_CONTEXT_FORBIDDEN")
        issuer = getattr(self._core, "issue_runtime_context", None)
        if not callable(issuer):
            raise TemperatureFormalRunnerError("RUNNER_CORE_CONTEXT_ISSUER_MISSING")
        context = issuer()
        if type(context) is not CoreResolvedSupervisedContextV2:
            raise TemperatureFormalRunnerError("RUNNER_CORE_CONTEXT_ISSUANCE_INVALID")
        return context

    def _build_packet(
        self,
        *,
        batch_index: int,
        tasks: tuple[PrivateChemBench4KTask, ...],
        pre: tuple[EvaluatedCandidateV1, ...],
        evidence: RuleEvidenceIndexV1,
        diagnostics: tuple[BatchAggregateDiagnosticV1, ...],
        context: CoreResolvedSupervisedContextV2 | None,
    ) -> Any:
        cases = tuple(
            BatchTrainCaseV1.from_evaluation(
                task=task,
                evaluation=result.evaluation,
                train_ordinal=(batch_index - 1) * BATCH_SIZE + index,
                batch_position=index + 1,
                task_request_sha256=result.accepted.plan.task_request_sha256,
                transcript_sha256=result.accepted.observed.transcript_sha256,
            )
            for index, (task, result) in enumerate(zip(tasks, pre, strict=True))
        )
        artifacts = _artifact_snapshots(self._core.head, context=context)
        seen = frozenset(self._admission.split.plan.train_uids[: batch_index * BATCH_SIZE])
        return build_batch_supervised_packet_v1(
            run_id=self.layout.controller_run_id,
            source_commit=self._admission.source_commit,
            split_sha256=self._admission.split.plan.split_sha256,
            config_sha256=self._admission.config.digest,
            dataset_sha256=self._admission.split.dataset_combined_sha256,
            batch_index=batch_index,
            current_train_cases=cases,
            current_artifacts=artifacts,
            prior_evidence=evidence,
            prior_batch_diagnostics=diagnostics,
            all_train_uids=frozenset(self._admission.split.plan.train_uids),
            seen_train_uids=seen,
        )

    def _phase_workspace(
        self,
        *,
        phase: str,
        batch_index: int | None,
        context: CoreResolvedSupervisedContextV2,
    ) -> Path:
        digest = sha256_bytes(canonical_json_bytes(context.to_runtime_payload()))
        arm_root = (
            self.layout.evolved_root if phase == "evolved_test" else self.layout.run_root
        )
        token = "test" if batch_index is None else f"batch-{batch_index}"
        return (arm_root / f"workspaces/{phase}-{token}-{digest[:16]}").resolve()

    def _write_call_checkpoint(self, envelope: FormalCallEnvelopeV1) -> None:
        path = self.layout.private_root / f"calls/{envelope.call_id}.json"
        payload = envelope.to_private_checkpoint_bytes()
        if path.exists():
            if _read_private_bytes(path) != payload:
                raise TemperatureFormalRunnerError("RUNNER_CALL_CHECKPOINT_DRIFT")
            return
        write_private_file(path, payload, replace=False)

    def _load_call_checkpoint(self, call_id: str) -> FormalCallEnvelopeV1:
        path = self.layout.private_root / f"calls/{call_id}.json"
        if not path.exists():
            raise TemperatureFormalRunnerError("RUNNER_CALL_CHECKPOINT_MISSING")
        return FormalCallEnvelopeV1.from_private_checkpoint_bytes(_read_private_bytes(path))

    def _save_core_checkpoint(self) -> None:
        _atomic_private_bytes(
            self.layout.core_checkpoint_path,
            self._core.to_private_checkpoint_bytes(),
        )

    def _core_state_checkpoint_path(self, batch_index: int) -> Path:
        return self.layout.private_root / f"core_checkpoints/C{batch_index}.json"

    def _write_core_state_checkpoint(self, state: CoreEvolutionStateV1) -> None:
        _write_private_once(
            self._core_state_checkpoint_path(state.batch_index),
            state.to_private_checkpoint_bytes(),
        )

    def _load_core_state_checkpoint(self, batch_index: int) -> CoreEvolutionStateV1:
        path = self._core_state_checkpoint_path(batch_index)
        if not path.exists():
            raise TemperatureFormalRunnerError("RUNNER_CORE_STATE_CHECKPOINT_MISSING")
        try:
            state = CoreEvolutionStateV1.from_private_checkpoint_bytes(
                _read_private_bytes(path)
            )
        except (ValueError, TemperatureCoreEvolutionError) as exc:
            raise TemperatureFormalRunnerError(
                "RUNNER_CORE_STATE_CHECKPOINT_INVALID"
            ) from exc
        if state.batch_index != batch_index:
            raise TemperatureFormalRunnerError("RUNNER_CORE_STATE_CHECKPOINT_DRIFT")
        return state

    def _ensure_core_state_checkpoint_chain(self) -> None:
        head_index = self._core.head.batch_index
        for batch_index in range(head_index + 1):
            path = self._core_state_checkpoint_path(batch_index)
            if not path.exists():
                if batch_index != head_index:
                    raise TemperatureFormalRunnerError(
                        "RUNNER_HISTORICAL_CORE_STATE_CHECKPOINT_MISSING"
                    )
                self._write_core_state_checkpoint(self._core.head)
            state = self._load_core_state_checkpoint(batch_index)
            if batch_index == 0:
                if state.target_receipts or state.committed_batch is not None:
                    raise TemperatureFormalRunnerError("RUNNER_C0_STATE_NOT_EMPTY")
            else:
                predecessor = self._load_core_state_checkpoint(batch_index - 1)
                if state.predecessor_state_sha256 != predecessor.digest:
                    raise TemperatureFormalRunnerError(
                        "RUNNER_CORE_STATE_PREDECESSOR_DRIFT"
                    )
        if self._load_core_state_checkpoint(head_index).digest != self._core.head.digest:
            raise TemperatureFormalRunnerError("RUNNER_CORE_HEAD_CHECKPOINT_DRIFT")

    def _load_closed_diagnostics(self) -> list[BatchAggregateDiagnosticV1]:
        values: list[BatchAggregateDiagnosticV1] = []
        for batch in range(1, 5):
            path = self.layout.private_root / f"batch_metrics/batch_{batch}.json"
            if not path.exists():
                break
            observed = BatchAggregateDiagnosticV1.model_validate_json(
                _read_private_bytes(path)
            )
            state = self._load_core_state_checkpoint(batch)
            expected = _batch_diagnostic_from_ledger(
                run_id=self.layout.controller_run_id,
                batch_index=batch,
                events=self._ledger.events,
                artifact_total_utf8_bytes=state.total_artifact_utf8_bytes,
            )
            if observed != expected:
                raise TemperatureFormalRunnerError("RUNNER_BATCH_METRICS_DRIFT")
            evidence = self._load_evidence(batch)
            expected_report = _train_batch_aggregate_payload(
                batch_index=batch,
                diagnostic=expected,
                evidence=evidence,
                state=state,
                context=None,
                ledger_events=self._ledger.events,
            )
            report_path = self.layout.private_root / f"batch_reports/batch_{batch}.json"
            _write_private_once(report_path, canonical_json_bytes(expected_report))
            if _load_private_json(report_path) != expected_report:
                raise TemperatureFormalRunnerError("RUNNER_BATCH_REPORT_DRIFT")
            values.append(observed)
        return values

    def _load_evidence(self, batch_index: int) -> RuleEvidenceIndexV1:
        if batch_index == 0:
            return RuleEvidenceIndexV1.generation_zero()
        path = self.layout.private_root / f"evidence/C{batch_index}.json"
        if not path.exists():
            raise TemperatureFormalRunnerError("RUNNER_EVIDENCE_CHECKPOINT_MISSING")
        evidence = RuleEvidenceIndexV1.model_validate_json(_read_private_bytes(path))
        if evidence.batch_index != batch_index:
            raise TemperatureFormalRunnerError("RUNNER_EVIDENCE_CHECKPOINT_DRIFT")
        state_path = self._core_state_checkpoint_path(batch_index)
        if state_path.exists():
            state = self._load_core_state_checkpoint(batch_index)
            if (
                state.committed_batch is None
                or state.committed_batch.evidence_sha256 != evidence.digest
            ):
                raise TemperatureFormalRunnerError(
                    "RUNNER_EVIDENCE_CORE_STATE_BINDING_DRIFT"
                )
        return evidence

    def _write_batch_report(
        self,
        *,
        batch_index: int,
        diagnostic: BatchAggregateDiagnosticV1,
        evidence: RuleEvidenceIndexV1,
        state: CoreEvolutionStateV1,
        context: CoreResolvedSupervisedContextV2,
    ) -> None:
        payload = _train_batch_aggregate_payload(
            batch_index=batch_index,
            diagnostic=diagnostic,
            evidence=evidence,
            state=state,
            context=context,
            ledger_events=self._ledger.events,
        )
        _write_private_once(
            self.layout.private_root / f"batch_reports/batch_{batch_index}.json",
            canonical_json_bytes(payload),
        )

    def _write_batch_checkpoints(
        self,
        *,
        batch_index: int,
        diagnostic: BatchAggregateDiagnosticV1,
        evidence: RuleEvidenceIndexV1,
        state: CoreEvolutionStateV1,
        context: CoreResolvedSupervisedContextV2,
    ) -> None:
        _write_private_once(
            self.layout.private_root / f"batch_metrics/batch_{batch_index}.json",
            canonical_json_bytes(diagnostic.model_dump(mode="json")),
        )
        self._write_batch_report(
            batch_index=batch_index,
            diagnostic=diagnostic,
            evidence=evidence,
            state=state,
            context=context,
        )

    def _load_batch_report(self, batch_index: int) -> dict[str, Any]:
        path = self.layout.private_root / f"batch_reports/batch_{batch_index}.json"
        if not path.exists():
            raise TemperatureFormalRunnerError("RUNNER_BATCH_REPORT_MISSING")
        value = _load_private_json(path)
        if value.get("batch_index") != batch_index:
            raise TemperatureFormalRunnerError("RUNNER_BATCH_REPORT_DRIFT")
        return value

    def _initialize_or_recover_layout(self) -> None:
        manifest = _run_manifest_payload(
            layout=self.layout,
            admission=self._admission,
            runtime=self._runtime,
        )
        if self._resume:
            if not self.layout.controller_manifest_path.exists():
                raise TemperatureFormalRunnerError("RUNNER_RESUME_MANIFEST_MISSING")
            existing = _load_private_json(self.layout.controller_manifest_path)
            for key, value in manifest.items():
                if key != "started_at_utc" and existing.get(key) != value:
                    raise TemperatureFormalRunnerError("RUNNER_RESUME_MANIFEST_DRIFT")
            self._started_at_utc = str(existing["started_at_utc"])
            for role, run_id, root, context_mode in (
                (
                    "evolved_test",
                    self.layout.evolved_run_id,
                    self.layout.evolved_root,
                    "frozen_C4",
                ),
                (
                    "baseline_test",
                    self.layout.baseline_run_id,
                    self.layout.baseline_root,
                    "C0_empty",
                ),
            ):
                expected_arm = _arm_manifest_payload(
                    layout=self.layout,
                    role=role,
                    run_id=run_id,
                    context_mode=context_mode,
                    admission=self._admission,
                    runtime=self._runtime,
                )
                if _load_private_json(root / "arm_manifest_v1.json") != expected_arm:
                    raise TemperatureFormalRunnerError("RUNNER_ARM_MANIFEST_DRIFT")
                if not (root / "evolution.sqlite3").is_file():
                    raise TemperatureFormalRunnerError("RUNNER_ARM_DATABASE_MISSING")
            return
        if self.layout.run_root.exists():
            raise TemperatureFormalRunnerError("RUNNER_FRESH_RUN_ROOT_EXISTS")
        for path in (
            self.layout.private_root,
            self.layout.run_root / "status",
            self.layout.core_root,
            self.layout.evolved_root,
            self.layout.baseline_root,
        ):
            path.mkdir(parents=True, mode=0o700, exist_ok=True)
            path.chmod(0o700)
        self._started_at_utc = _utc_seconds()
        manifest["started_at_utc"] = self._started_at_utc
        write_private_file(
            self.layout.controller_manifest_path,
            canonical_pretty_json_bytes(manifest),
            replace=False,
        )
        registry = load_verified_framework_registry(
            self.layout.repository_root / self._admission.config.framework_lock_relative
        )
        for role, run_id, root, context_mode in (
            ("evolved_test", self.layout.evolved_run_id, self.layout.evolved_root, "frozen_C4"),
            ("baseline_test", self.layout.baseline_run_id, self.layout.baseline_root, "C0_empty"),
        ):
            arm_store = EvolutionStore(
                db_path=root / "evolution.sqlite3",
                artifact_root=root / "artifacts",
                executable_registry=registry,
            )
            arm_store.initialize()
            (root / "workspaces").mkdir(mode=0o700, exist_ok=False)
            write_private_file(
                root / "arm_manifest_v1.json",
                canonical_pretty_json_bytes(
                    _arm_manifest_payload(
                        layout=self.layout,
                        role=role,
                        run_id=run_id,
                        context_mode=context_mode,
                        admission=self._admission,
                        runtime=self._runtime,
                    )
                ),
                replace=False,
            )

    def _close_audit_and_write_aggregate_input(
        self,
        *,
        diagnostics: tuple[BatchAggregateDiagnosticV1, ...],
        evidence: RuleEvidenceIndexV1,
        final_state: CoreEvolutionStateV1,
        context: CoreResolvedSupervisedContextV2,
        evolved: tuple[EvaluatedCandidateV1, ...],
        baseline: tuple[EvaluatedCandidateV1, ...],
    ) -> Path:
        path = self.layout.private_root / "aggregate_report_input_v1.json"
        plan_path = self.layout.private_root / "audit_closure_plan_v1.json"
        if plan_path.exists():
            plan = _load_private_json(plan_path)
            if set(plan) != {
                "schema_version",
                "completed_at_utc",
                "aggregate_report_input_sha256",
            } or plan.get("schema_version") != "TemperatureAuditClosurePlanV1":
                raise TemperatureFormalRunnerError("RUNNER_AUDIT_CLOSURE_PLAN_INVALID")
            completed_at_utc = str(plan["completed_at_utc"])
        else:
            completed_at_utc = _utc_seconds()
        payload = _aggregate_report_payload(
            layout=self.layout,
            admission=self._admission,
            runtime=self._runtime,
            started_at_utc=self._started_at_utc,
            completed_at_utc=completed_at_utc,
            ledger_events=self._ledger.events,
            diagnostics=diagnostics,
            evidence=evidence,
            final_state=final_state,
            context=context,
            evolved=evolved,
            baseline=baseline,
        )
        report = AggregateReportInputV1.model_validate(payload)
        encoded = canonical_pretty_json_bytes(report.model_dump(mode="json"))
        aggregate_sha256 = sha256_bytes(encoded)
        expected_plan = {
            "schema_version": "TemperatureAuditClosurePlanV1",
            "completed_at_utc": completed_at_utc,
            "aggregate_report_input_sha256": aggregate_sha256,
        }
        _write_private_once(plan_path, canonical_json_bytes(expected_plan))
        return _stage_and_close_aggregate_input(
            encoded=encoded,
            aggregate_sha256=aggregate_sha256,
            aggregate_path=path,
            receipt_path=(
                self.layout.private_root / "audit_closed_aggregate_receipt_v1.json"
            ),
            completed_at_utc=completed_at_utc,
            layout=self.layout,
            controller=self._controller,
            ledger=self._ledger,
        )


def _stage_and_close_aggregate_input(
    *,
    encoded: bytes,
    aggregate_sha256: str,
    aggregate_path: Path,
    receipt_path: Path,
    completed_at_utc: str,
    layout: TemperatureRunLayoutV1,
    controller: Any,
    ledger: Any,
) -> Path:
    """Stage READY input, fsync AUDIT_CLOSED, then seal a runner-side receipt."""

    if (
        type(encoded) is not bytes
        or len(encoded) < 2
        or len(encoded) > 2 * 1024 * 1024
        or sha256_bytes(encoded) != aggregate_sha256
    ):
        raise TemperatureFormalRunnerError("RUNNER_AGGREGATE_STAGING_INVALID")
    if aggregate_path.exists():
        if _read_private_bytes(aggregate_path) != encoded:
            raise TemperatureFormalRunnerError("RUNNER_AGGREGATE_REPORT_INPUT_DRIFT")
    else:
        _write_private_once(aggregate_path, encoded)
    audit_events = [event for event in ledger.events if event["kind"] == "AUDIT_CLOSED"]
    expected_payload = {
        "checksums_verified": True,
        "aggregate_report_input_sha256": aggregate_sha256,
    }
    if not audit_events:
        controller.append_protocol_event("AUDIT_CLOSED", expected_payload)
        audit_events = [ledger.events[-1]]
    if len(audit_events) != 1 or audit_events[0]["payload"] != expected_payload:
        raise TemperatureFormalRunnerError("RUNNER_AUDIT_CLOSURE_DRIFT")
    receipt = {
        "schema_version": "TemperatureAuditClosedAggregateReceiptV1",
        "controller_run_id": layout.controller_run_id,
        "all_run_ids": list(layout.all_run_ids),
        "ledger_audit_event_sha256": audit_events[0]["event_sha256"],
        "aggregate_report_input_sha256": aggregate_sha256,
        "completed_at_utc": completed_at_utc,
    }
    _write_private_once(receipt_path, canonical_json_bytes(receipt))
    return aggregate_path


def _validated_admission_roots(
    *,
    repository: Path,
    preflight_root: Path,
    private_split_root: Path,
) -> tuple[Path, Path]:
    namespace = (
        repository / "state/chembench_temperature_full_evolve_v1/preflights"
    ).resolve(strict=True)
    raw_public = preflight_root.absolute()
    raw_private = private_split_root.absolute()
    try:
        public = raw_public.resolve(strict=True)
        private = raw_private.resolve(strict=True)
        public_relative = public.relative_to(namespace)
        private_relative = private.relative_to(namespace)
    except (OSError, ValueError) as exc:
        raise TemperatureFormalRunnerError("RUNNER_ADMISSION_ROOT_OUTSIDE_STATE") from exc
    if (
        public != raw_public
        or private != raw_private
        or len(public_relative.parts) < 2
        or len(private_relative.parts) < 2
        or public_relative.parts[0] != private_relative.parts[0]
        or public == private
        or public.is_relative_to(private)
        or private.is_relative_to(public)
    ):
        raise TemperatureFormalRunnerError("RUNNER_ADMISSION_ROOT_NAMESPACE_INVALID")
    for root in (public, private, private / "private"):
        metadata = root.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o022
        ):
            raise TemperatureFormalRunnerError("RUNNER_ADMISSION_ROOT_UNSAFE")
    if stat.S_IMODE((private / "private").stat().st_mode) != 0o700:
        raise TemperatureFormalRunnerError("RUNNER_PRIVATE_SPLIT_DIRECTORY_NOT_OWNER_ONLY")
    return public, private


def _load_and_verify_admission(
    *,
    repository: Path,
    preflight_root: Path,
    private_split_root: Path,
    runtime: TemperatureRuntimeServicesIdentityV1,
) -> FormalAdmissionEvidenceV1:
    """Recompute every frozen input and bind it to the zero-call preflight."""

    config = load_temperature_full_evolve_config(
        repository
        / "benchmarks/chembench/configs/temperature_full_evolve_v1/"
        "temperature_full_evolve_v1.yaml"
    )
    source_commit = _git(repository, "rev-parse", "HEAD")
    branch = _git(repository, "rev-parse", "--abbrev-ref", "HEAD")
    if _COMMIT.fullmatch(source_commit) is None or re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", branch, re.ASCII
    ) is None:
        raise TemperatureFormalRunnerError("RUNNER_SOURCE_IDENTITY_INVALID")
    tracked = _git(repository, "status", "--porcelain", "--untracked-files=no")
    formal = _git(
        repository,
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--",
        "src/openevo",
        "benchmarks/chembench/src",
        "benchmarks/chembench/configs/temperature_full_evolve_v1",
        "benchmarks/chembench/scripts/temperature_full_evolve_v1",
        "benchmarks/chembench/tests/temperature_full_evolve_v1",
    )
    if tracked or formal:
        raise TemperatureFormalRunnerError("RUNNER_SOURCE_TREE_NOT_FROZEN")
    if runtime.source_commit != source_commit:
        raise TemperatureFormalRunnerError("RUNNER_RUNTIME_SOURCE_COMMIT_MISMATCH")
    try:
        formal_runtime = load_temperature_formal_runtime_v1(
            repository_root=repository
        )
    except TemperatureFormalRuntimeError as exc:
        raise TemperatureFormalRunnerError(
            "RUNNER_FORMAL_RUNTIME_IDENTITY_INVALID"
        ) from exc
    formal_runtime_receipt = formal_runtime.public_receipt
    if (
        formal_runtime_receipt.get("source_commit") != source_commit
        or formal_runtime.framework_lock
        != repository / config.framework_lock_relative
        or runtime.framework_lock_sha256
        != formal_runtime_receipt.get("framework_lock_sha256")
        or runtime.framework_wheel_sha256
        != formal_runtime_receipt.get("core_wheel_sha256")
    ):
        raise TemperatureFormalRunnerError(
            "RUNNER_FORMAL_RUNTIME_IDENTITY_MISMATCH"
        )

    names = {
        "preflight_report": "preflight_report.json",
        "split_manifest": "split_manifest.json",
        "historical_manifest": "historical_exclusion_manifest.json",
        "grouping_manifest": "near_duplicate_group_manifest.json",
        "config_manifest": "config_manifest.json",
        "model_identity_receipt": "model_identity_receipt.json",
        "runtime_identity_receipt": "managed_runtime_receipt.json",
        "regression_receipt": "regression_receipt_v1.json",
    }
    loaded = {
        key: _load_public_json(preflight_root / name) for key, name in names.items()
    }
    try:
        regression = RegressionReceiptV1.model_validate(loaded["regression_receipt"])
    except ValueError as exc:
        raise TemperatureFormalRunnerError("RUNNER_REGRESSION_RECEIPT_INVALID") from exc
    report = loaded["preflight_report"]
    if (
        report.get("status") != "PASS_READY_FOR_FORMAL_EXECUTION"
        or report.get("protocol_id") != PROTOCOL_ID
        or report.get("preflight_model_calls") != 0
        or report.get("source_commit") != source_commit
        or report.get("config_sha256") != config.digest
        or report.get("regression_receipt_sha256") != regression.digest
    ):
        raise TemperatureFormalRunnerError("RUNNER_PREFLIGHT_REPORT_MISMATCH")

    dataset_root = repository / str(config.payload["dataset"]["root"])
    split = build_temperature_split_v1(
        ChemBench4KDatasetLoader(snapshot_root=dataset_root)
    )
    rendered = render_temperature_split_artifacts_v1(split)
    expected_private = {
        "train": rendered.train_private_manifest,
        "test": rendered.test_private_manifest,
        "reserve": rendered.reserve_private_manifest,
    }
    for partition, expected in expected_private.items():
        actual = _read_private_bytes(
            private_split_root / f"private/{partition}_manifest.jsonl"
        )
        if actual != expected:
            raise TemperatureFormalRunnerError("RUNNER_PRIVATE_SPLIT_MANIFEST_DRIFT")
    public_receipt_path = private_split_root / "public/split_receipt.json"
    if public_receipt_path.read_bytes() != rendered.public_receipt:
        raise TemperatureFormalRunnerError("RUNNER_PUBLIC_SPLIT_RECEIPT_DRIFT")
    if (
        report.get("split_sha256") != split.plan.split_sha256
        or loaded["split_manifest"].get("split_sha256")
        != split.plan.split_sha256
        or loaded["split_manifest"].get("dataset_sha256")
        != split.dataset_combined_sha256
        or loaded["grouping_manifest"].get("group_manifest_sha256")
        != split.plan.group_manifest_sha256
        or loaded["grouping_manifest"].get("group_assignment_sha256")
        != split.plan.group_assignment_sha256
    ):
        raise TemperatureFormalRunnerError("RUNNER_SPLIT_IDENTITY_MISMATCH")
    model_receipt = loaded["model_identity_receipt"]
    runtime_receipt = loaded["runtime_identity_receipt"]
    if (
        model_receipt.get("model") != "gpt-5.5"
        or model_receipt.get("reasoning_effort") != "medium"
        or model_receipt.get("candidate_baseline_identity_equal") is not True
        or model_receipt.get("model_calls") != 0
        or model_receipt.get("candidate_codex_executable_sha256")
        != "a96f944d1a596dbfb7fdd84f482be5c50e34b04bb371126840d873e4ebf26902"
        or model_receipt.get("reflector_codex_executable_sha256")
        != "a96f944d1a596dbfb7fdd84f482be5c50e34b04bb371126840d873e4ebf26902"
        or runtime_receipt.get("runtime_services_identity_sha256") != runtime.digest
        or runtime_receipt.get("model_calls") != 0
        or runtime_receipt.get("framework_lock_sha256")
        != runtime.framework_lock_sha256
    ):
        raise TemperatureFormalRunnerError("RUNNER_INFERENCE_IDENTITY_MISMATCH")
    formal_runtime_keys = (
        "formal_runtime_identity_sha256",
        "formal_runtime_receipt_sha256",
        "source_commit",
        "source_tree_sha256",
        "core_wheel_sha256",
        "chembench_wheel_sha256",
        "core_editable",
        "chembench_editable",
    )
    preflight_key_by_formal_key = {
        "formal_runtime_identity_sha256": "formal_runtime_identity_sha256",
        "formal_runtime_receipt_sha256": "formal_runtime_receipt_sha256",
        "source_commit": "formal_runtime_source_commit",
        "source_tree_sha256": "formal_runtime_source_tree_sha256",
        "core_wheel_sha256": "core_wheel_sha256",
        "chembench_wheel_sha256": "chembench_wheel_sha256",
        "core_editable": "core_editable",
        "chembench_editable": "chembench_editable",
    }
    if any(
        runtime_receipt.get(preflight_key_by_formal_key[key])
        != formal_runtime_receipt.get(key)
        for key in formal_runtime_keys
    ) or runtime_receipt.get("formal_runtime_python_isolated") is not True:
        raise TemperatureFormalRunnerError(
            "RUNNER_FORMAL_RUNTIME_PREFLIGHT_MISMATCH"
        )
    runtime_body = {
        key: value
        for key, value in runtime_receipt.items()
        if key != "runtime_preflight_sha256"
    }
    try:
        runtime_preflight = RuntimePreflightEvidenceV1.model_validate(runtime_body)
    except ValueError as exc:
        raise TemperatureFormalRunnerError("RUNNER_RUNTIME_RECEIPT_INVALID") from exc
    if runtime_receipt.get("runtime_preflight_sha256") != runtime_preflight.digest:
        raise TemperatureFormalRunnerError("RUNNER_RUNTIME_RECEIPT_DIGEST_MISMATCH")
    rebuilt = build_zero_model_preflight_v1(
        config=config,
        split=split,
        runtime=runtime_preflight,
        regression=regression,
        source_commit=source_commit,
        source_tree_clean=True,
    )
    expected = {
        "preflight_report": rebuilt.report,
        "split_manifest": rebuilt.split_manifest,
        "historical_manifest": rebuilt.historical_manifest,
        "grouping_manifest": rebuilt.grouping_manifest,
        "config_manifest": rebuilt.config_manifest,
        "model_identity_receipt": rebuilt.model_identity_receipt,
        "runtime_identity_receipt": rebuilt.runtime_identity_receipt,
        "regression_receipt": rebuilt.regression_receipt,
    }
    _require_exact_preflight_payloads(loaded, expected)
    protocol_source = (
        repository
        / "benchmarks/chembench/configs/temperature_full_evolve_v1/EXPERIMENT_PROTOCOL.md"
    )
    try:
        if (preflight_root / "EXPERIMENT_PROTOCOL.md").read_bytes() != protocol_source.read_bytes():
            raise TemperatureFormalRunnerError("RUNNER_PROTOCOL_SOURCE_DRIFT")
    except OSError as exc:
        raise TemperatureFormalRunnerError("RUNNER_PROTOCOL_SOURCE_UNAVAILABLE") from exc
    return FormalAdmissionEvidenceV1(
        config=config,
        split=split,
        preflight_root=preflight_root,
        preflight_report=report,
        split_manifest=loaded["split_manifest"],
        historical_manifest=loaded["historical_manifest"],
        grouping_manifest=loaded["grouping_manifest"],
        config_manifest=loaded["config_manifest"],
        model_identity_receipt=model_receipt,
        runtime_identity_receipt=runtime_receipt,
        regression_receipt=regression,
        preflight_bundle_sha256=rebuilt.digest,
        source_commit=source_commit,
        branch=branch,
    )


def _run_manifest_payload(
    *,
    layout: TemperatureRunLayoutV1,
    admission: FormalAdmissionEvidenceV1,
    runtime: TemperatureRuntimeServicesIdentityV1,
) -> dict[str, Any]:
    return {
        "schema_version": RUN_MANIFEST_SCHEMA,
        "protocol_id": PROTOCOL_ID,
        "controller_run_id": layout.controller_run_id,
        "evolved_run_id": layout.evolved_run_id,
        "baseline_run_id": layout.baseline_run_id,
        "core_run_id": layout.core_run_id,
        "runtime_service_run_id": runtime.service_run_id,
        "source_commit": admission.source_commit,
        "branch": admission.branch,
        "config_sha256": admission.config.digest,
        "split_sha256": admission.split.plan.split_sha256,
        "preflight_bundle_sha256": admission.preflight_bundle_sha256,
        "runtime_services_identity_sha256": runtime.digest,
        "generation_zero_context_empty": True,
        "old_artifact_imported": False,
        "old_completion_imported": False,
        "old_database_imported": False,
        "old_workspace_imported": False,
    }


def _require_exact_preflight_payloads(
    loaded: dict[str, dict[str, Any]],
    expected: dict[str, dict[str, Any]],
) -> None:
    required = {
        "preflight_report",
        "split_manifest",
        "historical_manifest",
        "grouping_manifest",
        "config_manifest",
        "model_identity_receipt",
        "runtime_identity_receipt",
        "regression_receipt",
    }
    if set(loaded) != required or set(expected) != required or loaded != expected:
        raise TemperatureFormalRunnerError("RUNNER_PREFLIGHT_BUNDLE_DRIFT")


def _validate_dev_demonstrations(
    split: FrozenTemperatureSplitV1,
    dev: tuple[PrivateChemBench4KTask, ...],
) -> None:
    if (
        len(dev) != split.dev_demonstration_count
        or sha256_bytes(canonical_json_bytes(tuple(task.uid for task in dev)))
        != split.dev_demonstration_set_sha256
    ):
        raise TemperatureFormalRunnerError("RUNNER_DEV_DEMONSTRATION_IDENTITY_DRIFT")
    dev_ordered = {_prompt_content_signature(task, ordered=True) for task in dev}
    dev_unordered = {_prompt_content_signature(task, ordered=False) for task in dev}
    if (
        sha256_bytes(canonical_json_bytes(tuple(sorted(dev_ordered))))
        != split.dev_demonstration_content_set_sha256
    ):
        raise TemperatureFormalRunnerError("RUNNER_DEV_DEMONSTRATION_CONTENT_DRIFT")
    for tasks in split.partitions.values():
        if (
            {task.uid for task in dev} & {task.uid for task in tasks}
            or dev_ordered
            & {_prompt_content_signature(task, ordered=True) for task in tasks}
            or dev_unordered
            & {_prompt_content_signature(task, ordered=False) for task in tasks}
        ):
            raise TemperatureFormalRunnerError("RUNNER_DEV_SPLIT_OVERLAP")


def _prompt_content_signature(
    task: PrivateChemBench4KTask, *, ordered: bool
) -> str:
    options = [normalize_benchmark_text(getattr(task, label)) for label in "ABCD"]
    if not ordered:
        options.sort()
    return sha256_bytes(
        canonical_json_bytes([normalize_benchmark_text(task.question), *options])
    )


def _arm_manifest_payload(
    *,
    layout: TemperatureRunLayoutV1,
    role: str,
    run_id: str,
    context_mode: str,
    admission: FormalAdmissionEvidenceV1,
    runtime: TemperatureRuntimeServicesIdentityV1,
) -> dict[str, Any]:
    return {
        "schema_version": ARM_MANIFEST_SCHEMA,
        "run_id": run_id,
        "parent_controller_run_id": layout.controller_run_id,
        "role": role,
        "database_relative": "evolution.sqlite3",
        "workspace_root_relative": "workspaces",
        "context_mode": context_mode,
        "source_commit": admission.source_commit,
        "config_sha256": admission.config.digest,
        "split_sha256": admission.split.plan.split_sha256,
        "runtime_services_identity_sha256": runtime.digest,
        "feedback_enabled": False,
        "reflector_enabled": False,
        "core_evolution_enabled": False,
        "old_artifact_imported": False,
        "old_completion_imported": False,
        "old_database_imported": False,
        "old_workspace_imported": False,
    }


def _git(repository: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ("git", *arguments),
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TemperatureFormalRunnerError("RUNNER_GIT_IDENTITY_UNAVAILABLE") from exc
    return completed.stdout.strip()


def _load_public_json(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size < 2
            or metadata.st_size > 8 * 1024 * 1024
        ):
            raise OSError("unsafe public receipt")
        payload = path.read_bytes()
    except OSError as exc:
        raise TemperatureFormalRunnerError("RUNNER_PUBLIC_RECEIPT_UNAVAILABLE") from exc
    return _decode_json_object(payload, "RUNNER_PUBLIC_RECEIPT_INVALID")


def _decode_json_object(payload: bytes, finding: str) -> dict[str, Any]:
    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(
            payload,
            object_pairs_hook=unique_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("non-finite")
            ),
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise TemperatureFormalRunnerError(finding) from exc
    if type(value) is not dict:
        raise TemperatureFormalRunnerError(finding)
    return value


def _read_private_bytes(path: Path, *, maximum_bytes: int = 128 * 1024 * 1024) -> bytes:
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
            or metadata.st_size < 1
            or metadata.st_size > maximum_bytes
        ):
            raise OSError("unsafe private file")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            current = os.fstat(descriptor)
            if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise OSError("private file changed")
            chunks: list[bytes] = []
            remaining = metadata.st_size
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            if len(payload) != metadata.st_size:
                raise OSError("private file short read")
            return payload
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise TemperatureFormalRunnerError("RUNNER_PRIVATE_FILE_INVALID") from exc


def _load_private_json(path: Path) -> dict[str, Any]:
    return _decode_json_object(
        _read_private_bytes(path),
        "RUNNER_PRIVATE_JSON_INVALID",
    )


def _atomic_private_bytes(path: Path, payload: bytes) -> None:
    if not path.is_absolute() or type(payload) is not bytes or not payload:
        raise TypeError("private atomic write inputs are invalid")
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.parent.chmod(0o700)
    if path.exists() and _read_private_bytes(path) == payload:
        return
    token = hashlib.sha256(
        f"{path}:{os.getpid()}:{datetime.now(UTC).timestamp()}".encode()
    ).hexdigest()[:24]
    temporary = path.parent / f".{path.name}.{token}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written < 1:
                raise OSError("short private write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _write_private_once(path: Path, payload: bytes) -> None:
    """Create immutable owner-private evidence or verify exact prior bytes."""

    if path.exists():
        if _read_private_bytes(path) != payload:
            raise TemperatureFormalRunnerError("RUNNER_IMMUTABLE_EVIDENCE_DRIFT")
        return
    write_private_file(path, payload, replace=False)


def _ensure_core_dataset_binding_v1(
    *,
    store: EvolutionStore,
    rendered: Any,
    artifact_root: Path,
    intent_path: Path,
    binding_path: Path,
) -> CoreTextMemoryDatasetBindingV1:
    """Register or recover one exact managed Core input dataset.

    The immutable intent precedes payload/DB side effects. If the process dies
    after Core commits ``register_artifact`` but before the binding is written,
    the next invocation discovers and validates that one exact row instead of
    registering a duplicate.
    """

    from openevo_chembench.temperature_full_evolve_v1.core_evolution import (
        RenderedCoreTextMemoryDatasetV1,
    )

    if (
        type(store) is not EvolutionStore
        or type(rendered) is not RenderedCoreTextMemoryDatasetV1
        or not isinstance(artifact_root, Path)
        or not artifact_root.is_absolute()
        or not isinstance(intent_path, Path)
        or not intent_path.is_absolute()
        or not isinstance(binding_path, Path)
        or not binding_path.is_absolute()
        or intent_path == binding_path
        or store.files.root != artifact_root
    ):
        raise TemperatureFormalRunnerError("RUNNER_CORE_DATASET_ARGUMENT_INVALID")
    root_identity = _core_artifact_root_identity_v1(artifact_root)
    expected_relative = (
        Path("input_datasets")
        / PROTOCOL_ID
        / f"batch_{rendered.batch_index}"
    )
    dataset_directory = rendered.manifest_path.parent
    if (
        rendered.records_path.parent != dataset_directory
        or rendered.manifest_path.name != "manifest.json"
        or rendered.records_path.name != "records.jsonl"
    ):
        raise TemperatureFormalRunnerError("RUNNER_CORE_DATASET_PLACEMENT_INVALID")
    try:
        relative = dataset_directory.relative_to(artifact_root)
    except ValueError as exc:
        raise TemperatureFormalRunnerError(
            "RUNNER_CORE_DATASET_PLACEMENT_INVALID"
        ) from exc
    if relative != expected_relative:
        raise TemperatureFormalRunnerError("RUNNER_CORE_DATASET_PLACEMENT_INVALID")
    _validate_core_dataset_path_chain_v1(
        artifact_root=artifact_root,
        dataset_directory=dataset_directory,
        payload_paths=(rendered.manifest_path, rendered.records_path),
        require_payloads=False,
    )
    request = rendered.artifact_request()
    request_payload = request.model_dump(mode="json")
    intent = {
        "schema_version": "TemperatureCoreDatasetRegistrationIntentV1",
        "batch_index": rendered.batch_index,
        "artifact_root_identity": root_identity,
        "dataset_directory_relative": relative.as_posix(),
        "artifact_request_sha256": sha256_bytes(
            canonical_json_bytes(request_payload)
        ),
        "artifact_name": rendered.artifact_name,
        "manifest_uri": rendered.manifest_path.as_uri(),
        "records_uri": rendered.records_path.as_uri(),
        "manifest_sha256": sha256_bytes(rendered.manifest_bytes),
        "manifest_utf8_bytes": len(rendered.manifest_bytes),
        "records_sha256": sha256_bytes(rendered.records_bytes),
        "records_utf8_bytes": len(rendered.records_bytes),
    }
    intent_bytes = canonical_json_bytes(intent)
    if intent_path.exists():
        existing_intent_bytes = _read_private_bytes(intent_path, maximum_bytes=64 * 1024)
        existing_intent = _decode_json_object(
            existing_intent_bytes,
            "RUNNER_CORE_DATASET_INTENT_INVALID",
        )
        if existing_intent.get("artifact_root_identity") != root_identity:
            raise TemperatureFormalRunnerError(
                "RUNNER_CORE_DATASET_ROOT_IDENTITY_MISMATCH"
            )
        if existing_intent != intent or existing_intent_bytes != intent_bytes:
            raise TemperatureFormalRunnerError("RUNNER_CORE_DATASET_INTENT_DRIFT")
    else:
        _write_private_once(intent_path, intent_bytes)

    rendered.write_private()
    _validate_core_dataset_path_chain_v1(
        artifact_root=artifact_root,
        dataset_directory=dataset_directory,
        payload_paths=(rendered.manifest_path, rendered.records_path),
        require_payloads=True,
    )
    if _core_artifact_root_identity_v1(artifact_root) != root_identity:
        raise TemperatureFormalRunnerError(
            "RUNNER_CORE_DATASET_ROOT_IDENTITY_MISMATCH"
        )

    existing_binding: CoreTextMemoryDatasetBindingV1 | None = None
    if binding_path.exists():
        binding_bytes = _read_private_bytes(binding_path, maximum_bytes=64 * 1024)
        binding_payload = _decode_json_object(
            binding_bytes,
            "RUNNER_CORE_DATASET_BINDING_INVALID",
        )
        try:
            existing_binding = CoreTextMemoryDatasetBindingV1.model_validate(
                binding_payload
            )
        except ValueError as exc:
            raise TemperatureFormalRunnerError(
                "RUNNER_CORE_DATASET_BINDING_INVALID"
            ) from exc
        if binding_bytes != canonical_json_bytes(
            existing_binding.model_dump(mode="json")
        ):
            raise TemperatureFormalRunnerError(
                "RUNNER_CORE_DATASET_BINDING_INVALID"
            )

    artifact = _find_core_dataset_registration_v1(
        store=store,
        rendered=rendered,
        artifact_root=artifact_root,
        expected_artifact_id=(
            existing_binding.artifact_id if existing_binding is not None else None
        ),
    )
    if artifact is None:
        if existing_binding is not None:
            raise TemperatureFormalRunnerError(
                "RUNNER_CORE_DATASET_BINDING_ARTIFACT_MISSING"
            )
        artifact = store.register_artifact(request)
        recovered = _find_core_dataset_registration_v1(
            store=store,
            rendered=rendered,
            artifact_root=artifact_root,
            expected_artifact_id=artifact.artifact_id,
        )
        if recovered is None:
            raise TemperatureFormalRunnerError(
                "RUNNER_CORE_DATASET_REGISTRATION_NOT_DURABLE"
            )
        artifact = recovered
    expected_binding = rendered.bind(artifact)
    if existing_binding is not None:
        if existing_binding != expected_binding:
            raise TemperatureFormalRunnerError(
                "RUNNER_CORE_DATASET_BINDING_DRIFT"
            )
        binding = existing_binding
    else:
        binding = expected_binding
        _write_private_once(
            binding_path,
            canonical_json_bytes(binding.model_dump(mode="json")),
        )
    if _core_artifact_root_identity_v1(artifact_root) != root_identity:
        raise TemperatureFormalRunnerError(
            "RUNNER_CORE_DATASET_ROOT_IDENTITY_MISMATCH"
        )
    return binding


def _find_core_dataset_registration_v1(
    *,
    store: EvolutionStore,
    rendered: Any,
    artifact_root: Path,
    expected_artifact_id: str | None,
) -> Any | None:
    """Return the unique exact artifact row for a deterministic dataset intent."""

    request = rendered.artifact_request()
    request_payload = request.model_dump(mode="json")
    try:
        with store.connect() as connection:
            rows = connection.execute(
                """
                SELECT artifact_id, type, name, version, state, uri,
                       manifest_path, manifest_json, lineage_json,
                       compatibility_json, scores_json, tags_json, promoted,
                       staging_job_id
                FROM artifacts
                WHERE name = ? OR uri = ?
                ORDER BY artifact_id
                LIMIT 3
                """,
                (request.name, request.uri),
            ).fetchall()
            dataset_count = connection.execute(
                "SELECT COUNT(*) FROM artifacts WHERE type = ?",
                (str(request_payload["type"]),),
            ).fetchone()[0]
    except Exception as exc:
        raise TemperatureFormalRunnerError(
            "RUNNER_CORE_DATASET_REGISTRATION_LOOKUP_FAILED"
        ) from exc
    if len(rows) > 1:
        raise TemperatureFormalRunnerError(
            "RUNNER_CORE_DATASET_REGISTRATION_AMBIGUOUS"
        )
    if not rows:
        if dataset_count != rendered.batch_index - 1:
            raise TemperatureFormalRunnerError(
                "RUNNER_CORE_DATASET_REGISTRATION_SET_MISMATCH"
            )
        return None
    if dataset_count != rendered.batch_index:
        raise TemperatureFormalRunnerError(
            "RUNNER_CORE_DATASET_REGISTRATION_SET_MISMATCH"
        )
    row = rows[0]
    artifact_id = row["artifact_id"]
    if (
        type(artifact_id) is not str
        or (expected_artifact_id is not None and artifact_id != expected_artifact_id)
        or row["type"] != request_payload["type"]
        or row["name"] != request.name
        or row["version"] != 1
        or row["state"] != "active"
        or row["uri"] != request.uri
        or row["promoted"] != 0
        or row["staging_job_id"] is not None
    ):
        raise TemperatureFormalRunnerError(
            "RUNNER_CORE_DATASET_REGISTRATION_IDENTITY_MISMATCH"
        )
    expected_manifest_path = store.files.artifact_manifest_path(
        str(request_payload["type"]),
        artifact_id,
    )
    try:
        expected_manifest_path.relative_to(artifact_root)
    except ValueError as exc:
        raise TemperatureFormalRunnerError(
            "RUNNER_CORE_DATASET_REGISTRATION_MANIFEST_MISMATCH"
        ) from exc
    if row["manifest_path"] != os.fspath(expected_manifest_path):
        raise TemperatureFormalRunnerError(
            "RUNNER_CORE_DATASET_REGISTRATION_MANIFEST_MISMATCH"
        )
    json_fields = {
        "manifest_json": request_payload["manifest"],
        "lineage_json": request_payload["lineage"],
        "compatibility_json": request_payload["compatibility"],
        "scores_json": request_payload["scores"],
        "tags_json": request_payload["tags"],
    }
    for field, expected in json_fields.items():
        if _decode_core_store_json_v1(row[field]) != expected:
            raise TemperatureFormalRunnerError(
                "RUNNER_CORE_DATASET_REGISTRATION_MANIFEST_MISMATCH"
            )
    expected_store_manifest = {
        "artifact_id": artifact_id,
        "type": request_payload["type"],
        "name": request.name,
        "uri": request.uri,
        "manifest": request_payload["manifest"],
        "lineage": request_payload["lineage"],
        "compatibility": request_payload["compatibility"],
        "scores": request_payload["scores"],
        "tags": request_payload["tags"],
        "promoted": False,
    }
    if _read_core_store_manifest_v1(expected_manifest_path) != expected_store_manifest:
        raise TemperatureFormalRunnerError(
            "RUNNER_CORE_DATASET_REGISTRATION_MANIFEST_MISMATCH"
        )
    try:
        artifact = store.get_artifact(artifact_id)
    except Exception as exc:
        raise TemperatureFormalRunnerError(
            "RUNNER_CORE_DATASET_BINDING_ARTIFACT_MISSING"
        ) from exc
    if (
        artifact.artifact_id != artifact_id
        or artifact.type != request.type
        or artifact.name != request.name
        or artifact.version != 1
        or str(artifact.state) != "active"
        or artifact.uri != request.uri
        or artifact.manifest != request.manifest
        or artifact.compatibility != request.compatibility
        or artifact.scores != request.scores
        or artifact.tags != request.tags
        or artifact.promoted
    ):
        raise TemperatureFormalRunnerError(
            "RUNNER_CORE_DATASET_REGISTRATION_DTO_MISMATCH"
        )
    return artifact


def _core_artifact_root_identity_v1(root: Path) -> dict[str, object]:
    try:
        metadata = root.lstat()
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise TemperatureFormalRunnerError(
            "RUNNER_CORE_DATASET_ROOT_IDENTITY_INVALID"
        ) from exc
    if (
        resolved != root
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise TemperatureFormalRunnerError(
            "RUNNER_CORE_DATASET_ROOT_IDENTITY_INVALID"
        )
    return {
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "owner_uid": metadata.st_uid,
        "mode": stat.S_IMODE(metadata.st_mode),
        "path_sha256": sha256_bytes(os.fspath(root).encode("utf-8")),
    }


def _validate_core_dataset_path_chain_v1(
    *,
    artifact_root: Path,
    dataset_directory: Path,
    payload_paths: tuple[Path, Path],
    require_payloads: bool,
) -> None:
    try:
        relative = dataset_directory.relative_to(artifact_root)
    except ValueError as exc:
        raise TemperatureFormalRunnerError(
            "RUNNER_CORE_DATASET_PLACEMENT_INVALID"
        ) from exc
    current = artifact_root
    for part in relative.parts:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if require_payloads:
                raise TemperatureFormalRunnerError(
                    "RUNNER_CORE_DATASET_PLACEMENT_INVALID"
                ) from None
            break
        except OSError as exc:
            raise TemperatureFormalRunnerError(
                "RUNNER_CORE_DATASET_PLACEMENT_INVALID"
            ) from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise TemperatureFormalRunnerError(
                "RUNNER_CORE_DATASET_PLACEMENT_INVALID"
            )
    for path in payload_paths:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            if require_payloads:
                raise TemperatureFormalRunnerError(
                    "RUNNER_CORE_DATASET_PAYLOAD_INVALID"
                ) from None
            continue
        except OSError as exc:
            raise TemperatureFormalRunnerError(
                "RUNNER_CORE_DATASET_PAYLOAD_INVALID"
            ) from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise TemperatureFormalRunnerError(
                "RUNNER_CORE_DATASET_PAYLOAD_INVALID"
            )


def _decode_core_store_json_v1(value: object) -> Any:
    if type(value) is not str or not 2 <= len(value.encode("utf-8")) <= 64 * 1024:
        raise TemperatureFormalRunnerError(
            "RUNNER_CORE_DATASET_REGISTRATION_MANIFEST_MISMATCH"
        )

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = item
        return result

    try:
        return json.loads(
            value,
            object_pairs_hook=unique_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("non-finite")
            ),
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise TemperatureFormalRunnerError(
            "RUNNER_CORE_DATASET_REGISTRATION_MANIFEST_MISMATCH"
        ) from exc


def _read_core_store_manifest_v1(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o022
            or not 2 <= metadata.st_size <= 128 * 1024
        ):
            raise OSError("unsafe Core artifact manifest")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            current = os.fstat(descriptor)
            if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise OSError("Core artifact manifest changed")
            payload = os.read(descriptor, metadata.st_size + 1)
        finally:
            os.close(descriptor)
        if len(payload) != metadata.st_size:
            raise OSError("Core artifact manifest short read")
    except OSError as exc:
        raise TemperatureFormalRunnerError(
            "RUNNER_CORE_DATASET_REGISTRATION_MANIFEST_MISMATCH"
        ) from exc
    return _decode_json_object(
        payload,
        "RUNNER_CORE_DATASET_REGISTRATION_MANIFEST_MISMATCH",
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_seconds() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _has_semantic_event(
    events: tuple[dict[str, Any], ...],
    kind: str,
    *,
    batch_index: int | None = None,
) -> bool:
    matches = [
        event
        for event in events
        if event.get("kind") == kind
        and (batch_index is None or event["payload"].get("batch_index") == batch_index)
    ]
    if len(matches) > 1:
        raise TemperatureFormalRunnerError("RUNNER_DUPLICATE_SEMANTIC_EVENT")
    return bool(matches)


def _candidate_logical_call_id(
    *, run_id: str, phase: str, batch_index: int | None, task_ordinal: int
) -> str:
    token = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:16]
    batch = "b00" if batch_index is None else f"b{batch_index:02d}"
    return f"temperature-{token}-{phase.replace('_', '-')}-{batch}-i{task_ordinal:03d}"


def _reflector_logical_call_id(run_id: str, batch_index: int) -> str:
    token = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:20]
    return f"temperature-reflector-{token}-b{batch_index:02d}"


def _batch_diagnostic(
    *,
    batch_index: int,
    pre: tuple[EvaluatedCandidateV1, ...],
    post: tuple[EvaluatedCandidateV1, ...],
    artifact_total_utf8_bytes: int,
) -> BatchAggregateDiagnosticV1:
    reference = tuple(item.evaluation.correct for item in pre)
    candidate = tuple(item.evaluation.correct for item in post)
    paired = paired_binary_metrics_v1(reference, candidate)
    return BatchAggregateDiagnosticV1(
        batch_index=batch_index,
        pre_correct=paired.reference_correct,
        post_correct=paired.candidate_correct,
        both_correct=paired.both_correct,
        pre_only_correct=paired.reference_only_correct,
        post_only_correct=paired.candidate_only_correct,
        both_wrong=paired.both_wrong,
        pre_parser_success=sum(item.evaluation.official.parsed for item in pre),
        post_parser_success=sum(item.evaluation.official.parsed for item in post),
        accuracy_delta_percentage_points=paired.delta_percentage_points,
        mcnemar_exact_p=paired.mcnemar_exact_p,
        artifact_total_utf8_bytes=artifact_total_utf8_bytes,
    )


def _batch_diagnostic_from_ledger(
    *,
    run_id: str,
    batch_index: int,
    events: tuple[dict[str, Any], ...],
    artifact_total_utf8_bytes: int,
) -> BatchAggregateDiagnosticV1:
    vectors: dict[str, tuple[bool, ...]] = {}
    parsers: dict[str, int] = {}
    start = (batch_index - 1) * BATCH_SIZE
    evaluations = {
        str(event["payload"].get("logical_call_id")): event["payload"]
        for event in events
        if event["kind"] == "CALL_EVALUATED"
    }
    for phase in ("train_pre", "train_post"):
        correctness: list[bool] = []
        parser_success = 0
        for ordinal in range(start, start + BATCH_SIZE):
            logical = _candidate_logical_call_id(
                run_id=run_id,
                phase=phase,
                batch_index=batch_index,
                task_ordinal=ordinal,
            )
            event = evaluations.get(logical)
            if event is None:
                raise TemperatureFormalRunnerError("RUNNER_BATCH_EVALUATION_MISSING")
            evaluation = _decode_json_object(
                str(event["evaluation_json"]).encode("utf-8"),
                "RUNNER_BATCH_EVALUATION_INVALID",
            )
            value = evaluation.get("correct")
            if (
                type(value) is not bool
                or evaluation.get("phase") != phase
                or evaluation.get("batch_index") != batch_index
                or evaluation.get("task_ordinal") != ordinal
            ):
                raise TemperatureFormalRunnerError("RUNNER_BATCH_EVALUATION_DRIFT")
            correctness.append(value)
            parser_success += evaluation.get("official_parse_status") == "parsed"
        vectors[phase] = tuple(correctness)
        parsers[phase] = parser_success
    paired = paired_binary_metrics_v1(vectors["train_pre"], vectors["train_post"])
    return BatchAggregateDiagnosticV1(
        batch_index=batch_index,
        pre_correct=paired.reference_correct,
        post_correct=paired.candidate_correct,
        both_correct=paired.both_correct,
        pre_only_correct=paired.reference_only_correct,
        post_only_correct=paired.candidate_only_correct,
        both_wrong=paired.both_wrong,
        pre_parser_success=parsers["train_pre"],
        post_parser_success=parsers["train_post"],
        accuracy_delta_percentage_points=paired.delta_percentage_points,
        mcnemar_exact_p=paired.mcnemar_exact_p,
        artifact_total_utf8_bytes=artifact_total_utf8_bytes,
    )


def _test_closure_payload(
    values: tuple[EvaluatedCandidateV1, ...], *, baseline: bool
) -> dict[str, Any]:
    if len(values) != TEST_COUNT:
        raise TemperatureFormalRunnerError("RUNNER_TEST_ARM_INCOMPLETE")
    payload: dict[str, Any] = {
        "accepted_count": TEST_COUNT,
        "evaluated_count": TEST_COUNT,
        "feedback_disabled": True,
        "reflector_calls": 0,
        "core_jobs": 0,
        "artifact_updates": 0,
    }
    if baseline:
        payload["context_empty"] = True
    return payload


def _frozen_context_target_payload(
    context: CoreResolvedSupervisedContextV2,
) -> dict[str, object]:
    binding = SupervisedSessionContextBindingV2.from_context(
        session_id="temp-frozen-c4-context",
        context=context,
    )
    targets = [target.to_dict() for target in binding.targets]
    if len(targets) != 3:
        raise TemperatureFormalRunnerError("RUNNER_FINAL_CONTEXT_BINDING_INVALID")
    return {
        "frozen_context_targets": targets,
        "frozen_context_targets_sha256": sha256_bytes(
            canonical_json_bytes(targets)
        ),
    }


def _artifact_snapshots(
    state: CoreEvolutionStateV1,
    *,
    context: CoreResolvedSupervisedContextV2 | None,
) -> tuple[ReflectorArtifactSnapshotV1, ...]:
    if state.batch_index == 0:
        if context is not None:
            raise TemperatureFormalRunnerError("RUNNER_C0_CONTEXT_NOT_EMPTY")
        return tuple(
            ReflectorArtifactSnapshotV1.generation_zero(target)
            for target in ("text_memory", "skill_bundle", "agent_system")
        )
    if context is None or state.committed_batch is None:
        raise TemperatureFormalRunnerError("RUNNER_ARTIFACT_CONTEXT_MISSING")
    contexts = {
        "text_memory": (
            context.memory.core_artifact_id,
            context.memory.artifact_payload_sha256,
            context.memory.context_resolution_digest,
            context.memory.resolved_memory_sha256,
            context.memory.markdown,
        ),
        "skill_bundle": (
            context.skill.core_artifact_id,
            context.skill.artifact_payload_sha256,
            context.skill.context_resolution_digest,
            context.skill.resolved_content_sha256,
            context.skill.markdown,
        ),
        "agent_system": (
            context.agent_system.core_artifact_id,
            context.agent_system.artifact_payload_sha256,
            context.agent_system.context_resolution_digest,
            context.agent_system.resolved_content_sha256,
            context.agent_system.markdown,
        ),
    }
    by_target = {receipt.target_id: receipt for receipt in state.target_receipts}
    return tuple(
        ReflectorArtifactSnapshotV1(
            target_id=target,  # type: ignore[arg-type]
            content=contexts[target][4],
            artifact_id=contexts[target][0],
            predecessor_artifact_id=(
                by_target[target].planned_job.spec.predecessor_artifact_id
            ),
            artifact_payload_sha256=contexts[target][1],
            context_resolution_digest=contexts[target][2],
            resolved_content_sha256=contexts[target][3],
            utf8_byte_count=len(contexts[target][4].encode("utf-8")),
        )
        for target in ("text_memory", "skill_bundle", "agent_system")
    )


def _context_aggregate(
    context: CoreResolvedSupervisedContextV2,
) -> dict[str, Any]:
    contents = {
        "text_memory": (
            context.memory.markdown,
            context.memory.resolved_memory_sha256,
        ),
        "skill_bundle": (
            context.skill.markdown,
            context.skill.resolved_content_sha256,
        ),
        "agent_system": (
            context.agent_system.markdown,
            context.agent_system.resolved_content_sha256,
        ),
    }
    sizes = {key: len(value[0].encode("utf-8")) for key, value in contents.items()}
    identity = [
        {"target_id": key, "sha256": contents[key][1], "utf8_bytes": sizes[key]}
        for key in ("text_memory", "skill_bundle", "agent_system")
    ]
    return {
        "text_memory_sha256": contents["text_memory"][1],
        "skill_bundle_sha256": contents["skill_bundle"][1],
        "agent_system_sha256": contents["agent_system"][1],
        "combined_context_sha256": sha256_bytes(canonical_json_bytes(identity)),
        "text_memory_bytes": sizes["text_memory"],
        "skill_bundle_bytes": sizes["skill_bundle"],
        "agent_system_bytes": sizes["agent_system"],
        "combined_context_bytes": sum(sizes.values()),
        "validation_passed": True,
    }


def _state_context_aggregate(state: CoreEvolutionStateV1) -> dict[str, Any]:
    binding = state.materialized_context
    if state.batch_index == 0 or binding is None:
        raise TemperatureFormalRunnerError("RUNNER_STATE_CONTEXT_BINDING_MISSING")
    targets = {
        target.target_id: target for target in binding.target_bindings
    }
    if set(targets) != {"text_memory", "skill_bundle", "agent_system"}:
        raise TemperatureFormalRunnerError("RUNNER_STATE_CONTEXT_TARGETS_INVALID")
    identity = [
        {
            "target_id": target,
            "sha256": targets[target].resolved_content_sha256,
            "utf8_bytes": targets[target].resolved_content_utf8_bytes,
        }
        for target in ("text_memory", "skill_bundle", "agent_system")
    ]
    return {
        "text_memory_sha256": targets["text_memory"].resolved_content_sha256,
        "skill_bundle_sha256": targets["skill_bundle"].resolved_content_sha256,
        "agent_system_sha256": targets["agent_system"].resolved_content_sha256,
        "combined_context_sha256": sha256_bytes(canonical_json_bytes(identity)),
        "text_memory_bytes": targets["text_memory"].resolved_content_utf8_bytes,
        "skill_bundle_bytes": targets["skill_bundle"].resolved_content_utf8_bytes,
        "agent_system_bytes": targets["agent_system"].resolved_content_utf8_bytes,
        "combined_context_bytes": sum(
            targets[target].resolved_content_utf8_bytes
            for target in ("text_memory", "skill_bundle", "agent_system")
        ),
        "validation_passed": True,
    }


def _rule_aggregate(evidence: RuleEvidenceIndexV1) -> dict[str, Any]:
    active = tuple(rule for rule in evidence.rules if rule.status != "retired")
    return {
        "active_rules": len(active),
        "supported_rules": sum(rule.status == "confirmed" for rule in active),
        "conflicted_rules": sum(rule.contradiction_count > 0 for rule in active),
        "retired_rules": sum(rule.status == "retired" for rule in evidence.rules),
        "support_count_total": sum(rule.support_count for rule in evidence.rules),
        "contradiction_count_total": sum(
            rule.contradiction_count for rule in evidence.rules
        ),
        "evidence_sha256": evidence.digest,
    }


def _phase_attempt_counts(
    events: tuple[dict[str, Any], ...],
    *,
    phases: frozenset[str],
    batch_index: int | None,
) -> tuple[int, int, int, int]:
    claims = [
        event
        for event in events
        if event["kind"] == "CALL_CLAIMED"
        and event["payload"].get("phase") in phases
        and _logical_batch_index(str(event["payload"].get("logical_call_id")))
        == batch_index
    ]
    logical = {str(event["payload"]["logical_call_id"]) for event in claims}
    call_ids = {str(event["payload"]["call_id"]) for event in claims}
    no_completion = sum(
        event["kind"] == "CALL_NO_COMPLETION_FAILURE"
        and str(event["payload"].get("call_id")) in call_ids
        for event in events
    )
    rejected = sum(
        event["kind"] == "CALL_REJECTED_COMPLETION"
        and str(event["payload"].get("call_id")) in call_ids
        for event in events
    )
    return (
        len(claims),
        len(claims) - len(logical),
        no_completion + rejected,
        rejected,
    )


def _logical_batch_index(logical_call_id: str) -> int | None:
    match = re.search(r"(?:^|-)b(0[1-4])(?:-|$)", logical_call_id, re.ASCII)
    return None if match is None else int(match.group(1))


def _train_batch_aggregate_payload(
    *,
    batch_index: int,
    diagnostic: BatchAggregateDiagnosticV1,
    evidence: RuleEvidenceIndexV1,
    state: CoreEvolutionStateV1,
    context: CoreResolvedSupervisedContextV2 | None,
    ledger_events: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    if (
        diagnostic.batch_index != batch_index
        or evidence.batch_index != batch_index
        or state.batch_index != batch_index
    ):
        raise TemperatureFormalRunnerError("RUNNER_BATCH_AGGREGATE_SEQUENCE_INVALID")
    _attempts, retries, failures, rejected = _phase_attempt_counts(
        ledger_events,
        phases=frozenset({"train-pre", "train-post", "train_reflector"}),
        batch_index=batch_index,
    )
    artifacts = _state_context_aggregate(state)
    if context is not None and _context_aggregate(context) != artifacts:
        raise TemperatureFormalRunnerError("RUNNER_STATE_CONTEXT_AGGREGATE_DRIFT")
    return {
        "batch_index": batch_index,
        "task_count": BATCH_SIZE,
        "pre_correct": diagnostic.pre_correct,
        "post_correct": diagnostic.post_correct,
        "both_correct": diagnostic.both_correct,
        "pre_only_correct": diagnostic.pre_only_correct,
        "post_only_correct": diagnostic.post_only_correct,
        "both_wrong": diagnostic.both_wrong,
        "pre_parser_success": diagnostic.pre_parser_success,
        "post_parser_success": diagnostic.post_parser_success,
        "pre_candidate_calls": BATCH_SIZE,
        "post_candidate_calls": BATCH_SIZE,
        "reflector_calls": 1,
        "core_jobs": 3,
        "failed_attempts": failures,
        "retries": retries,
        "rejected_attempts": rejected,
        "artifacts": artifacts,
        "rules": _rule_aggregate(evidence),
    }


def _test_arm_aggregate(
    *,
    arm: Literal["evolved", "baseline"],
    values: tuple[EvaluatedCandidateV1, ...],
    context: CoreResolvedSupervisedContextV2 | None,
    events: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    phase = "evolved-test" if arm == "evolved" else "baseline-test"
    attempts, retries, failures, rejected = _phase_attempt_counts(
        events,
        phases=frozenset({phase}),
        batch_index=None,
    )
    context_summary = (
        {
            "combined_context_bytes": 0,
            "combined_context_sha256": hashlib.sha256(b"").hexdigest(),
        }
        if context is None
        else _context_aggregate(context)
    )
    return {
        "arm": arm,
        "correctness": tuple(item.evaluation.correct for item in values),
        "accepted_completion_count": len(values),
        "official_parser_success_count": sum(
            item.evaluation.official.parsed for item in values
        ),
        "strict_parser_success_count": sum(
            item.evaluation.strict.parsed for item in values
        ),
        "logical_candidate_calls": len(values),
        "model_attempts": attempts,
        "failed_attempts": failures,
        "retries": retries,
        "rejected_attempts": rejected,
        "context_bytes": context_summary["combined_context_bytes"],
        "context_sha256": context_summary["combined_context_sha256"],
        "feedback_enabled": False,
        "reflector_calls": 0,
        "core_jobs": 0,
        "artifact_updates": 0,
    }


def _aggregate_report_payload(
    *,
    layout: TemperatureRunLayoutV1,
    admission: FormalAdmissionEvidenceV1,
    runtime: TemperatureRuntimeServicesIdentityV1,
    started_at_utc: str,
    completed_at_utc: str,
    ledger_events: tuple[dict[str, Any], ...],
    diagnostics: tuple[BatchAggregateDiagnosticV1, ...],
    evidence: RuleEvidenceIndexV1,
    final_state: CoreEvolutionStateV1,
    context: CoreResolvedSupervisedContextV2,
    evolved: tuple[EvaluatedCandidateV1, ...],
    baseline: tuple[EvaluatedCandidateV1, ...],
) -> dict[str, Any]:
    if (
        len(diagnostics) != 4
        or evidence.batch_index != 4
        or final_state.batch_index != 4
        or len(evolved) != TEST_COUNT
        or len(baseline) != TEST_COUNT
    ):
        raise TemperatureFormalRunnerError("RUNNER_AGGREGATE_INPUT_INCOMPLETE")
    batch_reports = tuple(
        _load_private_json(
            layout.private_root / f"batch_reports/batch_{batch_index}.json"
        )
        for batch_index in range(1, 5)
    )
    claims = [event for event in ledger_events if event["kind"] == "CALL_CLAIMED"]
    logical = {str(event["payload"]["logical_call_id"]) for event in claims}
    no_completion = sum(
        event["kind"] == "CALL_NO_COMPLETION_FAILURE" for event in ledger_events
    )
    rejected_completion = sum(
        event["kind"] == "CALL_REJECTED_COMPLETION" for event in ledger_events
    )
    incidents = sum(event["kind"] == "INCIDENT_OPENED" for event in ledger_events)
    if incidents or any(event["kind"] == "RUN_FAILED" for event in ledger_events):
        raise TemperatureFormalRunnerError("RUNNER_UNCLASSIFIED_INCIDENT_PRESENT")
    final_artifacts = _context_aggregate(context)
    final_receipt = final_state.to_public_receipt()
    lineage = {
        "state_sha256": final_state.digest,
        "predecessor_state_sha256": final_state.predecessor_state_sha256,
        "target_completion_receipts": [
            receipt.digest for receipt in final_state.target_receipts
        ],
    }
    regression = admission.regression_receipt
    return {
        "schema_version": "TemperatureFullEvolveAggregateReportInputV1",
        "protocol_id": PROTOCOL_ID,
        "status": "READY_FOR_AUDIT",
        "generated_at_utc": completed_at_utc,
        "started_at_utc": started_at_utc,
        "completed_at_utc": completed_at_utc,
        "train_post_is_same_item_supervised_diagnostic": True,
        "run_ids": {
            "controller_run_id": layout.controller_run_id,
            "evolved_run_id": layout.evolved_run_id,
            "baseline_run_id": layout.baseline_run_id,
            "core_run_id": layout.core_run_id,
            "runtime_service_run_id": runtime.service_run_id,
            "all_run_ids": (*layout.all_run_ids, runtime.service_run_id),
        },
        "identity": {
            "source_commit": admission.source_commit,
            "branch": admission.branch,
            "config_sha256": admission.config.digest,
            "split_sha256": admission.split.plan.split_sha256,
            "dataset_sha256": admission.split.dataset_combined_sha256,
            "group_manifest_sha256": admission.split.plan.group_manifest_sha256,
            "group_assignment_sha256": admission.split.plan.group_assignment_sha256,
            "framework_lock_sha256": runtime.framework_lock_sha256,
            "formal_runtime_identity_sha256": admission.runtime_identity_receipt[
                "formal_runtime_identity_sha256"
            ],
            "formal_runtime_receipt_sha256": admission.runtime_identity_receipt[
                "formal_runtime_receipt_sha256"
            ],
            "source_tree_sha256": admission.runtime_identity_receipt[
                "formal_runtime_source_tree_sha256"
            ],
            "core_wheel_sha256": admission.runtime_identity_receipt[
                "core_wheel_sha256"
            ],
            "chembench_wheel_sha256": admission.runtime_identity_receipt[
                "chembench_wheel_sha256"
            ],
            "core_editable": admission.runtime_identity_receipt["core_editable"],
            "chembench_editable": admission.runtime_identity_receipt[
                "chembench_editable"
            ],
            "runtime_services_identity_sha256": runtime.digest,
            "model_identity_receipt_sha256": _file_sha256(
                admission.preflight_root / "model_identity_receipt.json"
            ),
            "managed_runtime_receipt_sha256": _file_sha256(
                admission.preflight_root / "managed_runtime_receipt.json"
            ),
            "model": "gpt-5.5",
            "reasoning_effort": "medium",
            "managed_codex_executable_sha256": (
                admission.model_identity_receipt[
                    "candidate_codex_executable_sha256"
                ]
            ),
            "managed_candidate_image_id": admission.model_identity_receipt[
                "candidate_image_id"
            ],
            "capture_mode": "transcript",
            "parser_id": "first_uppercase_opencompass_compatible",
            "strict_parser_id": "strict_single_letter_parser",
            "evaluator_id": "ChemBench4KPrivateEvaluator",
            "candidate_baseline_stack_equal": True,
            "evolved_then_baseline_order": True,
            "test_feedback_enabled": False,
            "test_reflector_calls": 0,
            "test_core_jobs": 0,
            "test_artifact_updates": 0,
        },
        "historical_reuse": {
            "policy": "fixed_official_temperature_pool_fresh_c0",
            "protocol_fixed_at_local_date": "2026-08-02",
            "historical_exclusion_applied": False,
            "selected_items_may_have_historical_exposure": True,
            "historical_exposure_used_for_item_selection": False,
            "prior_artifacts_imported": 0,
            "prior_completions_imported": 0,
            "prior_databases_imported": 0,
            "prior_workspaces_imported": 0,
            "generation_zero_empty_context": True,
        },
        "split": {
            "train_count": TRAIN_COUNT,
            "test_count": TEST_COUNT,
            "reserve_count": len(admission.split.reserve),
            "batch_size": BATCH_SIZE,
            "batch_count": TRAIN_COUNT // BATCH_SIZE,
            "eligible_pool_count": TRAIN_COUNT + TEST_COUNT + len(admission.split.reserve),
            "group_count": admission.split.plan.group_count,
            "singleton_group_count": admission.split.plan.singleton_group_count,
            "maximum_group_size": admission.split.plan.maximum_group_size,
            "uid_overlap_count": 0,
            "normalized_text_overlap_count": 0,
            "normalized_option_overlap_count": 0,
            "source_group_fields_available": False,
            "source_group_overlap_count": None,
            "test_sealed_until_train_complete": True,
            "split_frozen_before_model_calls": True,
        },
        "preflight": {
            "preflight_id": f"temperature-preflight-{admission.preflight_bundle_sha256[:16]}",
            "preflight_bundle_sha256": admission.preflight_bundle_sha256,
            "model_calls": 0,
            "focused_test_count": regression.focused_test_count,
            "focused_failure_count": regression.focused_failure_count,
            "integration_test_count": regression.integration_test_count,
            "integration_failure_count": regression.integration_failure_count,
            "generation_zero_context_bytes": 0,
            "source_tree_frozen": True,
            "exactly_once_recovery_passed": True,
            "test_feedback_barrier_passed": True,
        },
        "train_batches": batch_reports,
        "evolved_test": _test_arm_aggregate(
            arm="evolved", values=evolved, context=context, events=ledger_events
        ),
        "baseline_test": _test_arm_aggregate(
            arm="baseline", values=baseline, context=None, events=ledger_events
        ),
        "calls": {
            "candidate_logical_calls": 400,
            "reflector_logical_calls": 4,
            "core_jobs": 12,
            "experimental_model_logical_calls": 404,
            "managed_runtime_session_attempts": len(claims),
            "readiness_passed_for_accepted_calls": 404,
            "canary_provider_attempts": {
                "status": "NOT_MEASURED",
                "lower_bound": 404,
            },
            "failed_attempts": no_completion + rejected_completion,
            "retries": len(claims) - len(logical),
            "rejected_attempts": rejected_completion,
            "recoveries": sum(
                event["kind"] == "RECOVERY_COMPLETED" for event in ledger_events
            ),
            "invalidated_formal_runs": 0,
            "failures": {
                "infrastructure_no_completion": no_completion,
                "parser_or_evaluator": 0,
                "orchestration": 0,
                "runtime_or_credential": 0,
                "duplicate_or_lease": 0,
                "artifact_or_schema": rejected_completion,
                "data_integrity": 0,
            },
        },
        "final_artifacts": {
            **final_artifacts,
            "final_state_id": "C4",
            "final_manifest_sha256": sha256_bytes(canonical_json_bytes(final_receipt)),
            "lineage_sha256": sha256_bytes(canonical_json_bytes(lineage)),
            "frozen_before_test": True,
        },
        "regression": {
            "focused_passed": regression.focused_test_count,
            "focused_failed": regression.focused_failure_count,
            "integration_passed": regression.integration_test_count,
            "integration_failed": regression.integration_failure_count,
            "test_command_sha256": regression.command_sha256,
            "test_output_sha256": regression.output_sha256,
            "model_calls_during_tests": regression.model_calls,
        },
    }
