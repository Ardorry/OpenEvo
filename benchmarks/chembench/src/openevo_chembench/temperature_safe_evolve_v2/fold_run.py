"""Four-batch forward-validated Safe-Evolve fold state machine."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from openevo.evolution.framework.builtins import VerifiedExecutableRegistry

from openevo_chembench.chembench4k_evaluation import ChemBench4KPrivateEvaluator
from openevo_chembench.chembench4k_models import PrivateChemBench4KTask
from openevo_chembench.chembench4k_prompt import render_official_five_shot_prompt
from openevo_chembench.supervised_transfer_v1.common import (
    canonical_json_bytes,
    sha256_bytes,
    write_private_file,
)
from openevo_chembench.temperature_full_evolve_v1.statistics import paired_binary_metrics_v1
from openevo_chembench.temperature_safe_evolve_v2.artifacts import (
    CanonicalEntryV2,
    SafeArtifactStateV2,
    SelectedTarget,
)
from openevo_chembench.temperature_safe_evolve_v2.candidate import (
    CandidateContextV2,
    TargetInjectionV2,
)
from openevo_chembench.temperature_safe_evolve_v2.config import SafeEvolveConfigV2
from openevo_chembench.temperature_safe_evolve_v2.core_evolution import (
    create_safe_core_store_v2,
    execute_provisional_update_v2,
    promote_safe_state_v2,
    read_safe_core_store_identity_v2,
)
from openevo_chembench.temperature_safe_evolve_v2.execution import (
    AcceptedPhysicalResultV2,
    SafeFormalExecutionV2,
)
from openevo_chembench.temperature_safe_evolve_v2.gates import (
    InfrastructureOutcomeV2,
    PairedGateInputV2,
    decide_deployment_v2,
    decide_promotion_v2,
    r0_continue_v2,
    retirement_reasons_v2,
)
from openevo_chembench.temperature_safe_evolve_v2.ledger import SafeExperimentLedgerV2
from openevo_chembench.temperature_safe_evolve_v2.packet import (
    ReflectorArmRecordV2,
    ReflectorTrainItemV2,
    build_reflector_packet_v2,
)
from openevo_chembench.temperature_safe_evolve_v2.retrieval import (
    RetrievalReceiptV2,
    retrieve_sparse_context_v2,
)
from openevo_chembench.temperature_safe_evolve_v2.runtime_services import (
    TemperatureRuntimeServicesIdentityV1,
)


class FoldRunError(RuntimeError):
    def __init__(self, finding_code: str) -> None:
        self.finding_code = finding_code
        super().__init__(finding_code)


@dataclass(frozen=True, slots=True, repr=False)
class LogicalArmResultV2:
    label: str
    accepted: tuple[AcceptedPhysicalResultV2, ...]
    retrieval: tuple[RetrievalReceiptV2 | None, ...]
    evaluations: tuple[dict[str, object], ...]
    infrastructure: InfrastructureOutcomeV2

    def __repr__(self) -> str:
        return f"LogicalArmResultV2(label={self.label!r}, <private-results-redacted>)"

    @property
    def correctness(self) -> tuple[bool, ...]:
        return tuple(bool(value["correct"]) for value in self.evaluations)


@dataclass(frozen=True, slots=True)
class FoldRunOutcomeV2:
    fold_id: str
    run_id: str
    promotion_count: int
    retirement_count: int
    deployment_real_evolved: bool
    test_metrics: dict[str, object]
    public_summary: dict[str, object]
    private_result_path: Path
    r0_continue: bool | None
    r0_stop_reasons: tuple[str, ...]
    runtime_services_run_id: str = ""
    runtime_services_identity_sha256: str = ""
    core_store_id: str = ""


class SafeFoldRunnerV2:
    def __init__(
        self,
        *,
        repository_root: Path,
        run_id: str,
        fold_id: Literal["R0", "R1", "R2", "R3"],
        run_root: Path,
        selected_target: SelectedTarget,
        train: tuple[PrivateChemBench4KTask, ...],
        validation: tuple[PrivateChemBench4KTask, ...],
        test: tuple[PrivateChemBench4KTask, ...],
        dev: tuple[PrivateChemBench4KTask, ...],
        config: SafeEvolveConfigV2,
        runtime: TemperatureRuntimeServicesIdentityV1,
        registry: VerifiedExecutableRegistry,
        resume: bool,
    ) -> None:
        if (
            len(train) != 100
            or len(validation) != 50
            or len(test) != 50
            or len(dev) != 5
            or len({task.uid for task in (*train, *validation, *test)}) != 200
        ):
            raise FoldRunError("SAFE_FOLD_PARTITION_INVALID")
        self.repository = repository_root
        self.run_id = run_id
        self.fold_id = fold_id
        self.root = run_root
        self.target = selected_target
        self.train = train
        self.validation = validation
        self.test = test
        self.dev = dev
        self.config = config
        self.runtime = runtime
        self.registry = registry
        if resume and self.root.exists():
            _require_private_directory(self.root)
        else:
            _fresh_private_directory(self.root)
        (self.root / "private").mkdir(mode=0o700, exist_ok=True)
        (self.root / "private").chmod(0o700)
        self.store = create_safe_core_store_v2(
            db_path=self.root / "core/evolution.sqlite3",
            artifact_root=self.root / "core/artifacts",
            registry=registry,
        )
        self.core_store_id = read_safe_core_store_identity_v2(
            db_path=self.root / "core/evolution.sqlite3"
        )

    def run(self) -> FoldRunOutcomeV2:
        with SafeExperimentLedgerV2(
            path=self.root / "private/events.jsonl", run_id=self.run_id
        ) as ledger:
            if not ledger.events:
                ledger.append(
                    "FOLD_RUN_CREATED",
                    {
                        "fold_id": self.fold_id,
                        "run_id": self.run_id,
                        "selected_target": self.target,
                        "train_count": 100,
                        "validation_count": 50,
                        "test_count": 50,
                        "fresh_generation_zero": True,
                        "runtime_services_run_id": self.runtime.service_run_id,
                        "runtime_services_identity_sha256": self.runtime.digest,
                        "core_store_id": self.core_store_id,
                    },
                )
            else:
                created = [
                    event["payload"]
                    for event in ledger.events
                    if event["kind"] == "FOLD_RUN_CREATED"
                ]
                if created != [
                    {
                        "fold_id": self.fold_id,
                        "run_id": self.run_id,
                        "selected_target": self.target,
                        "train_count": 100,
                        "validation_count": 50,
                        "test_count": 50,
                        "fresh_generation_zero": True,
                        "runtime_services_run_id": self.runtime.service_run_id,
                        "runtime_services_identity_sha256": self.runtime.digest,
                        "core_store_id": self.core_store_id,
                    }
                ]:
                    raise FoldRunError("SAFE_FOLD_IDENTITY_BINDING_DRIFT")
            engine = SafeFormalExecutionV2(
                runtime=self.runtime,
                ledger=ledger,
                checkpoint_root=self.root / "private/calls",
                cooldown_seconds=self.config.cooldown_seconds,
                candidate_logical_limit=500,
                reflector_logical_limit=4,
            )
            g0 = SafeArtifactStateV2.generation_zero(
                run_id=self.run_id, fold_id=self.fold_id, selected_target=self.target
            )
            active = g0
            pending: SafeArtifactStateV2 | None = None
            evidence: dict[str, object] = {
                "schema_version": "TemperatureSafeCumulativeEvidenceV2",
                "entry_evidence": {},
                "candidate_history": [],
                "mandatory_retirement_entry_ids": [],
            }
            promotion_history: list[dict[str, object]] = []
            forward_history: list[dict[str, object]] = []
            promotion_count = 0
            retired_entry_ids: set[str] = set()

            for batch_index in range(1, 5):
                batch = self.train[(batch_index - 1) * 25 : batch_index * 25]
                states: list[tuple[str, SafeArtifactStateV2]] = [("g0", g0), ("active", active)]
                if pending is not None:
                    states.append(("provisional", pending))
                block = self._run_block(
                    ledger=ledger,
                    engine=engine,
                    tasks=batch,
                    states=tuple(states),
                    phase="train",
                    block_id=f"b{batch_index}",
                    gt_barrier="ALL_LOGICAL_ARMS_ACCEPTED",
                )
                if pending is not None:
                    decision, receipt = self._promotion_decision(
                        reference=block["active"],
                        candidate=block["provisional"],
                        g0=block["g0"],
                        candidate_state=pending,
                        block_id=f"b{batch_index}",
                    )
                    if decision.promoted:
                        active = promote_safe_state_v2(
                            store=self.store,
                            state=pending,
                            evidence_path=self.root
                            / f"private/promotions/b{batch_index}/core_promotion.json",
                        )
                        promotion_count += 1
                    receipt["active_state_sha256_after_decision"] = active.digest
                    promotion_history.append(receipt)
                    forward_history.append(receipt["forward_metrics"])
                    _append_protocol_once(
                        ledger,
                        "PROMOTION_DECIDED",
                        receipt,
                        identity=("block_id", f"b{batch_index}"),
                    )
                    evidence_active_result = (
                        block["provisional"] if decision.promoted else block["active"]
                    )
                else:
                    evidence_active_result = block["active"]
                evidence_block = {**block, "active": evidence_active_result}
                evidence = _update_cumulative_evidence(
                    evidence=evidence,
                    active_state=active,
                    block=evidence_block,
                    block_id=f"b{batch_index}",
                )
                mandatory = _mandatory_retirements(active, evidence)
                evidence["mandatory_retirement_entry_ids"] = list(mandatory)
                seen_tasks = self.train[: batch_index * 25]
                items = tuple(
                    _reflector_item(task, block, local_index)
                    for local_index, task in enumerate(batch)
                )
                packet = build_reflector_packet_v2(
                    run_id=self.run_id,
                    fold_id=self.fold_id,
                    batch_index=batch_index,
                    selected_target=self.target,
                    active_state=active,
                    items=items,
                    cumulative_evidence=evidence,
                    promotion_history=tuple(promotion_history),
                    aggregate_forward_history=tuple(forward_history),
                    seen_train_uids=frozenset(task.uid for task in seen_tasks),
                    seen_train_tasks=seen_tasks,
                    test_tasks=(*self.validation, *self.test),
                )
                reflection = engine.run_reflector(
                    packet=packet,
                    synthesis_path=self.root
                    / f"private/reflector/batch_{batch_index}/accepted_synthesis.json",
                )
                entries = tuple(
                    CanonicalEntryV2.model_validate(value)
                    for value in reflection.merged_entries_payload
                )
                reflector_sha = sha256_bytes(canonical_json_bytes(reflection.synthesis_receipt))
                update = execute_provisional_update_v2(
                    store=self.store,
                    registry=self.registry,
                    run_id=self.run_id,
                    fold_id=self.fold_id,
                    batch_index=batch_index,
                    selected_target=self.target,
                    predecessor=active,
                    entries=entries,
                    source_packet_sha256=packet.packet_sha256,
                    prior_evidence_sha256=packet.prior_evidence_sha256,
                    reflector_receipt_sha256=reflector_sha,
                    forbidden_normalized_questions=packet.forbidden_normalized_questions,
                    evidence_root=self.root / f"private/core_updates/batch_{batch_index}",
                )
                pending = update.state
                retired_entry_ids.update(
                    entry.entry_id for entry in pending.entries if entry.status == "retired"
                )
                evidence["candidate_history"].append(
                    {
                        "batch_index": batch_index,
                        "candidate_state_sha256": pending.digest,
                        "entries": [entry.model_dump(mode="json") for entry in pending.entries],
                    }
                )
                _append_protocol_once(
                    ledger,
                    "CORE_JOB_COMPLETED",
                    {
                        "batch_index": batch_index,
                        "job_id": pending.core_job_id,
                        "artifact_id": pending.core_artifact_id,
                        "validation_receipt_sha256": pending.core_validation_receipt_sha256,
                        "promoted": False,
                    },
                    identity=("batch_index", batch_index),
                )
                _append_protocol_once(
                    ledger,
                    "PROVISIONAL_STATE_FROZEN",
                    {
                        "batch_index": batch_index,
                        "state_sha256": pending.digest,
                        "projection_sha256": pending.projection_sha256,
                        "core_payload_sha256": pending.core_payload_sha256,
                        "core_payload_utf8_bytes": pending.core_payload_utf8_bytes,
                    },
                    identity=("batch_index", batch_index),
                )
                self._status(
                    ledger=ledger,
                    phase="TRAIN",
                    block=f"B{batch_index}",
                    active=active,
                    pending=pending,
                    promotions=promotion_count,
                    retired=len(retired_entry_ids),
                )

            if pending is None:
                raise FoldRunError("SAFE_FOLD_FINAL_PROVISIONAL_MISSING")
            v1 = self._run_block(
                ledger=ledger,
                engine=engine,
                tasks=self.validation[:25],
                states=(("g0", g0), ("active", active), ("provisional", pending)),
                phase="validation1",
                block_id="v1",
                gt_barrier="V1_ALL_ARMS_ACCEPTED",
            )
            decision, v1_receipt = self._promotion_decision(
                reference=v1["active"],
                candidate=v1["provisional"],
                g0=v1["g0"],
                candidate_state=pending,
                block_id="v1",
            )
            if decision.promoted:
                active = promote_safe_state_v2(
                    store=self.store,
                    state=pending,
                    evidence_path=self.root / "private/promotions/v1/core_promotion.json",
                )
                promotion_count += 1
            v1_receipt["active_state_sha256_after_decision"] = active.digest
            promotion_history.append(v1_receipt)
            forward_history.append(v1_receipt["forward_metrics"])
            _append_protocol_once(
                ledger, "PROMOTION_DECIDED", v1_receipt, identity=("block_id", "v1")
            )
            _append_protocol_once(
                ledger,
                "V1_CLOSED",
                {
                    "raw_active_state_sha256": active.digest,
                    "promotion_count": promotion_count,
                },
                identity=("raw_active_state_sha256", active.digest),
            )
            raw_active = active
            v2 = self._run_block(
                ledger=ledger,
                engine=engine,
                tasks=self.validation[25:],
                states=(("g0", g0), ("raw", raw_active)),
                phase="validation2",
                block_id="v2",
                gt_barrier="V2_ALL_ARMS_ACCEPTED",
            )
            v2_pair = PairedGateInputV2(
                v2["g0"].correctness,
                v2["raw"].correctness,
                v2["g0"].infrastructure,
                v2["raw"].infrastructure,
            )
            cumulative_utility = sum(
                int(value["forward_metrics"]["incremental_utility"])
                for value in promotion_history
                if value["promoted"]
            )
            deployment_audit = self._deployment_integrity_audit(
                ledger=ledger,
                raw_state=raw_active,
                v2=v2,
                promotion_history=tuple(promotion_history),
            )
            if not deployment_audit:
                raise FoldRunError("SAFE_FOLD_DEPLOYMENT_INTEGRITY_AUDIT_FAILED")
            deployment = decide_deployment_v2(
                versus_g0=v2_pair,
                cumulative_promoted_forward_utility=cumulative_utility,
                promotion_count=promotion_count,
                audit_passed=deployment_audit,
            )
            deployment_receipt = {
                "schema_version": "TemperatureSafeDeploymentDecisionV2",
                "block_id": "v2",
                "deployed_real_evolved": deployment.deployed_real_evolved,
                "fallback_to_g0": not deployment.deployed_real_evolved,
                "reasons": list(deployment.reasons),
                "positive_flips": v2_pair.positive_flips,
                "negative_flips": v2_pair.negative_flips,
                "utility": v2_pair.utility,
                "g0_correct": sum(v2["g0"].correctness),
                "raw_correct": sum(v2["raw"].correctness),
                "promotion_count": promotion_count,
                "cumulative_promoted_forward_utility": cumulative_utility,
                "raw_state_sha256": raw_active.digest,
                "artifact_retrieval_lineage_test_sealing_audit_passed": deployment_audit,
            }
            _append_protocol_once(
                ledger,
                "V2_CLOSED",
                deployment_receipt,
                identity=("block_id", "v2"),
            )
            _append_protocol_once(
                ledger,
                "FINAL_STATE_FROZEN",
                {
                    "raw_state_sha256": raw_active.digest,
                    "raw_artifact_id": raw_active.core_artifact_id,
                    "raw_core_payload_sha256": raw_active.core_payload_sha256,
                    "deployed_state": "raw" if deployment.deployed_real_evolved else "g0_fallback",
                    "feedback_closed": True,
                    "reflector_calls_after_freeze": 0,
                    "core_jobs_after_freeze": 0,
                },
                identity=("raw_state_sha256", raw_active.digest),
            )
            test_block = self._run_block(
                ledger=ledger,
                engine=engine,
                tasks=self.test,
                states=(("g0", g0), ("raw", raw_active)),
                phase="test",
                block_id="test",
                gt_barrier="TEST_BOTH_ARMS_ACCEPTED",
            )
            raw_pair = PairedGateInputV2(
                test_block["g0"].correctness,
                test_block["raw"].correctness,
                test_block["g0"].infrastructure,
                test_block["raw"].infrastructure,
            )
            deployed_vector = (
                test_block["raw"].correctness
                if deployment.deployed_real_evolved
                else test_block["g0"].correctness
            )
            deployed_metrics = paired_binary_metrics_v1(
                test_block["g0"].correctness,
                deployed_vector,
                bootstrap_seed=20260803,
            )
            raw_metrics = raw_pair.metrics
            test_metrics = {
                "schema_version": "TemperatureSafeFoldTestMetricsV2",
                "fold_id": self.fold_id,
                "run_id": self.run_id,
                "g0_correct": raw_metrics.reference_correct,
                "raw_correct": raw_metrics.candidate_correct,
                "deployed_correct": sum(deployed_vector),
                "n": 50,
                "raw_vs_g0": raw_metrics.to_dict(),
                "deployed_vs_g0": deployed_metrics.to_dict(),
                "positive_flips": raw_pair.positive_flips,
                "negative_flips": raw_pair.negative_flips,
                "utility": raw_pair.utility,
                "deployment_fallback": not deployment.deployed_real_evolved,
                "parser_retry_failure_parity": raw_pair.infrastructure_equal,
            }
            _append_protocol_once(
                ledger,
                "FOLD_TEST_CLOSED",
                {
                    "fold_id": self.fold_id,
                    "g0_correct": raw_metrics.reference_correct,
                    "raw_correct": raw_metrics.candidate_correct,
                    "deployed_correct": sum(deployed_vector),
                    "deployment_fallback": not deployment.deployed_real_evolved,
                    "reflector_calls": 4,
                    "core_jobs": 4,
                    "test_reflector_calls": 0,
                    "test_core_jobs": 0,
                    "test_artifact_updates": 0,
                },
                identity=("fold_id", self.fold_id),
            )
            r0_continue: bool | None = None
            r0_reasons: tuple[str, ...] = ()
            if self.fold_id == "R0":
                integrity_passed = self._test_integrity_audit(
                    ledger=ledger,
                    test_block=test_block,
                )
                if not integrity_passed:
                    raise FoldRunError("SAFE_FOLD_TEST_INTEGRITY_AUDIT_FAILED")
                r0_continue, r0_reasons = r0_continue_v2(
                    deployment=deployment,
                    raw_test=raw_pair,
                    promotion_count=promotion_count,
                    integrity_passed=integrity_passed,
                )
                _append_protocol_once(
                    ledger,
                    "R0_CONTINUATION_DECIDED",
                    {
                        "continue_r1_r3": r0_continue,
                        "status": "CONTINUE"
                        if r0_continue
                        else "NO_GO_R0_SAFE_EVOLVE_PILOT_FAILED",
                        "reasons": list(r0_reasons),
                    },
                    identity=(
                        "status",
                        "CONTINUE" if r0_continue else "NO_GO_R0_SAFE_EVOLVE_PILOT_FAILED",
                    ),
                )
            private = {
                "schema_version": "TemperatureSafeFoldPrivateResultV2",
                "fold_id": self.fold_id,
                "run_id": self.run_id,
                "selected_target": self.target,
                "runtime_services_run_id": self.runtime.service_run_id,
                "runtime_services_identity_sha256": self.runtime.digest,
                "core_store_id": self.core_store_id,
                "promotion_history": promotion_history,
                "cumulative_evidence": evidence,
                "deployment": deployment_receipt,
                "test": {
                    "uids": [task.uid for task in self.test],
                    "g0_correctness": list(test_block["g0"].correctness),
                    "raw_correctness": list(test_block["raw"].correctness),
                    "deployed_correctness": list(deployed_vector),
                    "g0_predictions": [
                        value["official_prediction"] for value in test_block["g0"].evaluations
                    ],
                    "raw_predictions": [
                        value["official_prediction"] for value in test_block["raw"].evaluations
                    ],
                },
                "test_metrics": test_metrics,
            }
            private_path = self.root / "private/fold_result.json"
            _persist_exact(private_path, canonical_json_bytes(private))
            public = {
                "schema_version": "TemperatureSafeFoldPublicSummaryV2",
                "fold_id": self.fold_id,
                "run_id": self.run_id,
                "selected_target": self.target,
                "runtime_services_run_id": self.runtime.service_run_id,
                "runtime_services_identity_sha256": self.runtime.digest,
                "core_store_id": self.core_store_id,
                "promotion_count": promotion_count,
                "retirement_count": len(retired_entry_ids),
                "v2_deployed_real_evolved": deployment.deployed_real_evolved,
                "test_metrics": test_metrics,
                "contains_uid": False,
                "contains_prediction": False,
                "contains_target": False,
            }
            _persist_exact(
                self.root / "private/fold_public_summary.json",
                canonical_json_bytes(public),
            )
            self._status(
                ledger=ledger,
                phase="FOLD_COMPLETE",
                block="TEST",
                active=raw_active,
                pending=None,
                promotions=promotion_count,
                retired=len(retired_entry_ids),
            )
            self._require_fold_closure(ledger)
            return FoldRunOutcomeV2(
                fold_id=self.fold_id,
                run_id=self.run_id,
                promotion_count=promotion_count,
                retirement_count=len(retired_entry_ids),
                deployment_real_evolved=deployment.deployed_real_evolved,
                test_metrics=test_metrics,
                public_summary=public,
                private_result_path=private_path,
                r0_continue=r0_continue,
                r0_stop_reasons=r0_reasons,
                runtime_services_run_id=self.runtime.service_run_id,
                runtime_services_identity_sha256=self.runtime.digest,
                core_store_id=self.core_store_id,
            )

    def _run_block(
        self,
        *,
        ledger: SafeExperimentLedgerV2,
        engine: SafeFormalExecutionV2,
        tasks: tuple[PrivateChemBench4KTask, ...],
        states: tuple[tuple[str, SafeArtifactStateV2], ...],
        phase: str,
        block_id: str,
        gt_barrier: str,
    ) -> dict[str, LogicalArmResultV2]:
        accepted_by_arm: dict[str, tuple[AcceptedPhysicalResultV2, ...]] = {}
        retrieval_by_arm: dict[str, tuple[RetrievalReceiptV2 | None, ...]] = {}
        before_evaluated = sum(event["kind"] == "CALL_EVALUATED" for event in ledger.events)
        for label, state in states:
            accepted: list[AcceptedPhysicalResultV2] = []
            receipts: list[RetrievalReceiptV2 | None] = []
            for ordinal, task in enumerate(tasks):
                context, retrieval = _context_for_task(task, state=state)
                workspace = (
                    None
                    if not (context.skill or context.agent_system)
                    else self.root
                    / "workspaces"
                    / f"{phase}-{block_id}-{context.binding_sha256[:16]}"
                )
                accepted.append(
                    engine.run_candidate(
                        task=task.to_public(),
                        prompt=render_official_five_shot_prompt(
                            task.to_public(), category_dev=self.dev
                        ),
                        context=context,
                        context_workspace=workspace,
                        phase=phase,
                        block_id=block_id,
                        task_ordinal=ordinal,
                        run_id=self.run_id,
                    )
                )
                receipts.append(retrieval)
            accepted_by_arm[label] = tuple(accepted)
            retrieval_by_arm[label] = tuple(receipts)
        if sum(event["kind"] == "CALL_EVALUATED" for event in ledger.events) != before_evaluated:
            raise FoldRunError("SAFE_BLOCK_GT_VISIBLE_BEFORE_BARRIER")
        _append_protocol_once(
            ledger,
            "BLOCK_INFERENCE_CLOSED",
            {
                "phase": phase,
                "block_id": block_id,
                "logical_arm_count": len(states),
                "task_count": len(tasks),
                "gt_visible_during_inference": False,
            },
            identity=("block_id", block_id),
        )
        if phase == "test":
            _append_protocol_once(
                ledger,
                "TEST_INFERENCE_CLOSED",
                {
                    "phase": phase,
                    "block_id": block_id,
                    "task_count": len(tasks),
                    "logical_arm_count": len(states),
                    "feedback_enabled": False,
                    "reflector_calls": 0,
                    "core_jobs": 0,
                    "artifact_updates": 0,
                },
                identity=("block_id", block_id),
            )
        _append_protocol_once(
            ledger,
            "GT_RELEASED" if phase != "test" else "TEST_GT_RELEASED",
            {
                "phase": phase,
                "block_id": block_id,
                "barrier": gt_barrier,
                "all_logical_arms_closed": True,
            },
            identity=("block_id", block_id),
        )
        existing = {
            str(event["payload"]["logical_call_id"]): json.loads(
                str(event["payload"]["evaluation_json"])
            )
            for event in ledger.events
            if event["kind"] == "CALL_EVALUATED"
        }
        results: dict[str, LogicalArmResultV2] = {}
        for label, _state in states:
            evaluations: list[dict[str, object]] = []
            for task, accepted in zip(tasks, accepted_by_arm[label], strict=True):
                payload = existing.get(accepted.logical_call_id)
                if payload is None:
                    payload = _evaluate(task, accepted)
                    encoded = canonical_json_bytes(payload).decode("utf-8")
                    ledger.append(
                        "CALL_EVALUATED",
                        {
                            "logical_call_id": accepted.logical_call_id,
                            "evaluation_json": encoded,
                            "evaluation_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
                        },
                    )
                    existing[accepted.logical_call_id] = payload
                evaluations.append(payload)
            results[label] = LogicalArmResultV2(
                label=label,
                accepted=accepted_by_arm[label],
                retrieval=retrieval_by_arm[label],
                evaluations=tuple(evaluations),
                infrastructure=_arm_infrastructure(
                    ledger, accepted_by_arm[label], tuple(evaluations)
                ),
            )
        _persist_exact(
            self.root / f"private/blocks/{phase}-{block_id}.json",
            canonical_json_bytes(
                {
                    "schema_version": "TemperatureSafePrivateBlockResultV2",
                    "phase": phase,
                    "block_id": block_id,
                    "arms": {
                        label: {
                            "evaluations": list(result.evaluations),
                            "retrieval": [
                                None if receipt is None else receipt.private_record()
                                for receipt in result.retrieval
                            ],
                        }
                        for label, result in results.items()
                    },
                }
            ),
        )
        return results

    def _require_fold_closure(self, ledger: SafeExperimentLedgerV2) -> None:
        candidate = ledger.accepted_logical_count("safe_candidate")
        reflector = ledger.accepted_logical_count("safe_reflector")
        core = sum(event["kind"] == "CORE_JOB_COMPLETED" for event in ledger.events)
        if candidate > 500 or reflector != 4 or core != 4:
            raise FoldRunError("SAFE_FOLD_CALL_BUDGET_OR_CLOSURE_INVALID")
        with self.store.connect() as connection:
            jobs = int(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
        if jobs != 4:
            raise FoldRunError("SAFE_FOLD_CORE_JOB_COUNT_INVALID")
        freeze = next(
            (
                index
                for index, event in enumerate(ledger.events)
                if event["kind"] == "FINAL_STATE_FROZEN"
            ),
            None,
        )
        if freeze is None or any(
            event["kind"] in {"REFLECTOR_ACCEPTED", "CORE_JOB_COMPLETED"}
            for event in ledger.events[freeze + 1 :]
        ):
            raise FoldRunError("SAFE_FOLD_POST_FREEZE_MUTATION_INVALID")

    def _deployment_integrity_audit(
        self,
        *,
        ledger: SafeExperimentLedgerV2,
        raw_state: SafeArtifactStateV2,
        v2: dict[str, LogicalArmResultV2],
        promotion_history: tuple[dict[str, object], ...],
    ) -> bool:
        try:
            self.runtime.require_current()
        except Exception:  # noqa: BLE001 - closed audit result, no exception detail escapes
            return False
        counts = {
            kind: sum(event["kind"] == kind for event in ledger.events)
            for kind in ("REFLECTOR_ACCEPTED", "CORE_JOB_COMPLETED", "PROMOTION_DECIDED")
        }
        with self.store.connect() as connection:
            core_jobs = int(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
            succeeded_jobs = int(
                connection.execute(
                    "SELECT COUNT(*) FROM jobs WHERE state = 'succeeded'"
                ).fetchone()[0]
            )
        hard_bytes = 1200 if self.target == "text_memory" else 700
        retrieval_safe = all(
            receipt is None or receipt.injected_utf8_bytes <= hard_bytes
            for result in v2.values()
            for receipt in result.retrieval
        )
        raw_state_safe = (
            raw_state.run_id == self.run_id
            and raw_state.fold_id == self.fold_id
            and raw_state.selected_target == self.target
            and len(raw_state.runtime_entries) <= 6
            and (
                raw_state.state_kind == "generation_zero"
                or (raw_state.state_kind == "active" and raw_state.promoted)
            )
        )
        if raw_state.state_kind == "active":
            raw_state_safe = raw_state_safe and any(
                value.get("promoted") is True
                and value.get("active_state_sha256_after_decision") == raw_state.digest
                for value in promotion_history
            )
        test_not_started = not any(
            event["kind"]
            in {"TEST_INFERENCE_CLOSED", "TEST_GT_RELEASED", "FOLD_TEST_CLOSED"}
            or (
                event["kind"] == "CALL_CLAIMED"
                and event["payload"].get("phase") == "inference-test"
            )
            for event in ledger.events
        )
        return (
            counts
            == {
                "REFLECTOR_ACCEPTED": 4,
                "CORE_JOB_COMPLETED": 4,
                "PROMOTION_DECIDED": 4,
            }
            and core_jobs == succeeded_jobs == 4
            and len(promotion_history) == 4
            and all(value.get("artifact_audit_passed") is True for value in promotion_history)
            and retrieval_safe
            and raw_state_safe
            and test_not_started
        )

    def _test_integrity_audit(
        self,
        *,
        ledger: SafeExperimentLedgerV2,
        test_block: dict[str, LogicalArmResultV2],
    ) -> bool:
        kinds = [str(event["kind"]) for event in ledger.events]
        try:
            freeze = kinds.index("FINAL_STATE_FROZEN")
            inference_closed = kinds.index("TEST_INFERENCE_CLOSED")
            gt_released = kinds.index("TEST_GT_RELEASED")
            fold_test_closed = kinds.index("FOLD_TEST_CLOSED")
        except ValueError:
            return False
        test_evaluations = [
            index
            for index, event in enumerate(ledger.events)
            if event["kind"] == "CALL_EVALUATED"
            and '"phase":"test"' in str(event["payload"].get("evaluation_json", ""))
        ]
        post_freeze_mutation = any(
            event["kind"] in {"REFLECTOR_ACCEPTED", "CORE_JOB_COMPLETED"}
            for event in ledger.events[freeze + 1 :]
        )
        return (
            freeze < inference_closed < gt_released < fold_test_closed
            and 50 <= len(test_evaluations) <= 100
            and all(index > gt_released for index in test_evaluations)
            and not post_freeze_mutation
            and set(test_block) == {"g0", "raw"}
            and all(
                len(value.accepted) == len(value.evaluations) == 50
                for value in test_block.values()
            )
            and sum(kind == "REFLECTOR_ACCEPTED" for kind in kinds) == 4
            and sum(kind == "CORE_JOB_COMPLETED" for kind in kinds) == 4
        )

    def _promotion_decision(
        self,
        *,
        reference: LogicalArmResultV2,
        candidate: LogicalArmResultV2,
        g0: LogicalArmResultV2,
        candidate_state: SafeArtifactStateV2,
        block_id: str,
    ) -> tuple[object, dict[str, object]]:
        incremental = PairedGateInputV2(
            reference.correctness,
            candidate.correctness,
            reference.infrastructure,
            candidate.infrastructure,
        )
        versus_g0 = PairedGateInputV2(
            g0.correctness,
            candidate.correctness,
            g0.infrastructure,
            candidate.infrastructure,
        )
        artifact_audit = (
            candidate_state.state_kind == "provisional"
            and not candidate_state.promoted
            and candidate_state.core_artifact_id is not None
            and candidate_state.core_validation_receipt_sha256 is not None
            and all(
                receipt is None
                or receipt.injected_utf8_bytes <= (1200 if self.target == "text_memory" else 700)
                for receipt in candidate.retrieval
            )
        )
        decision = decide_promotion_v2(
            incremental=incremental,
            versus_g0=versus_g0,
            artifact_audit_passed=artifact_audit,
        )
        receipt = {
            "schema_version": "TemperatureSafePromotionDecisionV2",
            "block_id": block_id,
            "candidate_state_sha256": candidate_state.digest,
            "promoted": decision.promoted,
            "reasons": list(decision.reasons),
            "forward_metrics": {
                "incremental_positive": incremental.positive_flips,
                "incremental_negative": incremental.negative_flips,
                "incremental_utility": incremental.utility,
                "incremental_accuracy_reference": incremental.metrics.reference_accuracy,
                "incremental_accuracy_candidate": incremental.metrics.candidate_accuracy,
                "baseline_positive": versus_g0.positive_flips,
                "baseline_negative": versus_g0.negative_flips,
                "baseline_utility": versus_g0.utility,
                "baseline_accuracy": versus_g0.metrics.reference_accuracy,
                "candidate_accuracy": versus_g0.metrics.candidate_accuracy,
            },
            "artifact_audit_passed": artifact_audit,
            "infrastructure_parity": incremental.infrastructure_equal
            and versus_g0.infrastructure_equal,
        }
        return decision, receipt

    def _status(
        self,
        *,
        ledger: SafeExperimentLedgerV2,
        phase: str,
        block: str,
        active: SafeArtifactStateV2,
        pending: SafeArtifactStateV2 | None,
        promotions: int,
        retired: int,
    ) -> None:
        counts: dict[str, int] = {}
        for event in ledger.events:
            counts[event["kind"]] = counts.get(event["kind"], 0) + 1
        payload = {
            "schema_version": "TemperatureSafeFoldStatusV2",
            "run_id": self.run_id,
            "fold_id": self.fold_id,
            "phase": phase,
            "block": block,
            "accepted_candidate_count": counts.get("CALL_ACCEPTED", 0)
            - counts.get("REFLECTOR_ACCEPTED", 0),
            "reflector_count": counts.get("REFLECTOR_ACCEPTED", 0),
            "core_job_count": counts.get("CORE_JOB_COMPLETED", 0),
            "active_state_sha256": active.digest,
            "provisional_state_sha256": None if pending is None else pending.digest,
            "promotion_count": promotions,
            "retirement_count": retired,
            "updated_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "runner_pid": os.getpid(),
        }
        write_private_file(self.root / "status.json", canonical_json_bytes(payload), replace=True)


def _context_for_task(
    task: PrivateChemBench4KTask, *, state: SafeArtifactStateV2
) -> tuple[CandidateContextV2, RetrievalReceiptV2 | None]:
    if state.state_kind == "generation_zero":
        return CandidateContextV2.generation_zero(), None
    retrieval = retrieve_sparse_context_v2(task.to_public(), state=state)
    if not retrieval.injected_payload:
        return CandidateContextV2.generation_zero(), retrieval
    if (
        state.core_artifact_id is None
        or state.core_payload_sha256 is None
        or state.core_payload_utf8_bytes is None
    ):
        raise FoldRunError("SAFE_SPARSE_CONTEXT_CORE_BINDING_MISSING")
    injection = TargetInjectionV2(
        target_id=state.selected_target,
        core_artifact_id=state.core_artifact_id,
        canonical_artifact_sha256=state.core_payload_sha256,
        canonical_artifact_utf8_bytes=state.core_payload_utf8_bytes,
        payload=retrieval.injected_payload,
        payload_sha256=retrieval.injected_payload_sha256,
        payload_utf8_bytes=retrieval.injected_utf8_bytes,
        retrieval_input_sha256=retrieval.retrieval_input_sha256,
        selected_entry_ids=tuple(item.entry_id for item in retrieval.selected),
    )
    return (
        CandidateContextV2(
            state_identity_sha256=state.digest,
            injections=(injection,),
            mode="safe_sparse",
        ),
        retrieval,
    )


def _evaluate(
    task: PrivateChemBench4KTask, accepted: AcceptedPhysicalResultV2
) -> dict[str, object]:
    evaluation = ChemBench4KPrivateEvaluator().evaluate(
        task=task, raw_completion=accepted.response
    )
    return {
        "schema_version": "TemperatureSafeCandidateEvaluationV2",
        "logical_call_id": accepted.logical_call_id,
        "task_uid": task.uid,
        "task_ordinal": accepted.task_ordinal,
        "phase": accepted.phase,
        "block_id": accepted.block_id,
        "context_hash": accepted.context_hash,
        "context_binding_sha256": accepted.context_binding_sha256,
        "official_prediction": evaluation.official.prediction,
        "official_parse_status": evaluation.official.status.value,
        "strict_prediction": evaluation.strict.prediction,
        "strict_parse_status": evaluation.strict.status.value,
        "correct": evaluation.correct,
        "response_sha256": accepted.response_sha256,
        "transcript_sha256": accepted.transcript_sha256,
    }


def _arm_infrastructure(
    ledger: SafeExperimentLedgerV2,
    accepted: tuple[AcceptedPhysicalResultV2, ...],
    evaluations: tuple[dict[str, object], ...],
) -> InfrastructureOutcomeV2:
    logical = {value.logical_call_id for value in accepted}
    claims = [
        event
        for event in ledger.events
        if event["kind"] == "CALL_CLAIMED" and event["payload"].get("logical_call_id") in logical
    ]
    failures = [
        event
        for event in ledger.events
        if event["kind"] == "CALL_NO_COMPLETION_FAILURE"
        and event["payload"].get("logical_call_id") in logical
    ]
    return InfrastructureOutcomeV2(
        accepted_count=len(accepted),
        parser_success_count=sum(
            value["official_parse_status"] == "parsed" for value in evaluations
        ),
        retry_count=len(claims) - len(logical),
        failure_count=len(failures),
        timeout_count=sum(
            "timeout" in str(event["payload"].get("terminal_task_status", "")).casefold()
            for event in failures
        ),
    )


def _reflector_item(
    task: PrivateChemBench4KTask,
    block: dict[str, LogicalArmResultV2],
    index: int,
) -> ReflectorTrainItemV2:
    arms = []
    for label in ("g0", "active", "provisional"):
        if label not in block:
            continue
        result = block[label]
        accepted = result.accepted[index]
        evaluation = result.evaluations[index]
        retrieval = result.retrieval[index]
        arms.append(
            ReflectorArmRecordV2(
                logical_label=label,
                response=accepted.response,
                response_sha256=accepted.response_sha256,
                parsed_prediction=evaluation["official_prediction"],  # type: ignore[arg-type]
                parser_status=str(evaluation["official_parse_status"]),
                correct=bool(evaluation["correct"]),
                context_hash=accepted.context_hash,
                selected_entry_ids=(
                    ()
                    if retrieval is None
                    else tuple(item.entry_id for item in retrieval.selected)
                ),
            )
        )
    return ReflectorTrainItemV2(task=task, arms=tuple(arms))


def _update_cumulative_evidence(
    *,
    evidence: dict[str, object],
    active_state: SafeArtifactStateV2,
    block: dict[str, LogicalArmResultV2],
    block_id: str,
) -> dict[str, object]:
    updated = json.loads(json.dumps(evidence))
    records = updated["entry_evidence"]
    if not isinstance(records, dict):
        raise FoldRunError("SAFE_CUMULATIVE_EVIDENCE_INVALID")
    if "g0" not in block or "active" not in block:
        return updated
    base = block["g0"]
    active = block["active"]
    per_entry_block: dict[str, tuple[int, int]] = {}
    for index, (base_ok, active_ok) in enumerate(
        zip(base.correctness, active.correctness, strict=True)
    ):
        retrieval = active.retrieval[index]
        if retrieval is None:
            continue
        uid = str(active.evaluations[index]["task_uid"])
        for selected in retrieval.selected:
            record = records.setdefault(
                selected.entry_id,
                {
                    "positive_flip_refs": [],
                    "negative_flip_refs": [],
                    "evaluated_blocks": [],
                    "block_utilities": [],
                    "last_support_block": None,
                },
            )
            positives, negatives = per_entry_block.get(selected.entry_id, (0, 0))
            if active_ok and not base_ok:
                if uid not in record["positive_flip_refs"]:
                    record["positive_flip_refs"].append(uid)
                record["last_support_block"] = block_id
                positives += 1
            elif base_ok and not active_ok:
                if uid not in record["negative_flip_refs"]:
                    record["negative_flip_refs"].append(uid)
                negatives += 1
            per_entry_block[selected.entry_id] = positives, negatives
    for entry in active_state.runtime_entries:
        record = records.setdefault(
            entry.entry_id,
            {
                "positive_flip_refs": [],
                "negative_flip_refs": [],
                "evaluated_blocks": [],
                "block_utilities": [],
                "last_support_block": None,
            },
        )
        if entry.entry_id in per_entry_block and block_id not in record["evaluated_blocks"]:
            positives, negatives = per_entry_block[entry.entry_id]
            record["evaluated_blocks"].append(block_id)
            record["block_utilities"].append(positives - 2 * negatives)
        record["positive_flip_refs"] = sorted(record["positive_flip_refs"])
        record["negative_flip_refs"] = sorted(record["negative_flip_refs"])
    return updated


def _mandatory_retirements(
    active: SafeArtifactStateV2, evidence: dict[str, object]
) -> tuple[str, ...]:
    records = evidence.get("entry_evidence")
    if not isinstance(records, dict):
        raise FoldRunError("SAFE_CUMULATIVE_EVIDENCE_INVALID")
    mandatory: list[str] = []
    for entry in active.runtime_entries:
        record = records.get(entry.entry_id, {})
        positives = tuple(record.get("positive_flip_refs", []))
        negatives = tuple(record.get("negative_flip_refs", []))
        utilities = tuple(int(value) for value in record.get("block_utilities", []))
        evaluated_blocks = tuple(record.get("evaluated_blocks", []))
        last_support = record.get("last_support_block")
        blocks_without_support = (
            len(evaluated_blocks)
            if last_support is None
            else max(0, len(evaluated_blocks) - evaluated_blocks.index(last_support) - 1)
            if last_support in evaluated_blocks
            else len(evaluated_blocks)
        )
        positive_refs = {*entry.positive_flip_refs, *positives}
        negative_refs = {*entry.negative_flip_refs, *negatives}
        enriched = entry.model_copy(
            update={
                "positive_flip_refs": tuple(sorted(positive_refs)),
                "negative_flip_refs": tuple(sorted(negative_refs)),
                "positive_flip_count": len(positive_refs),
                "negative_flip_count": len(negative_refs),
            }
        )
        reasons = retirement_reasons_v2(
            enriched,
            evaluated_block_utilities=utilities,
            blocks_without_new_support=blocks_without_support,
            stronger_unconditional_conflict=False,
            safety_failure=False,
        )
        if reasons:
            mandatory.append(entry.entry_id)
    return tuple(sorted(mandatory))


def _append_protocol_once(
    ledger: SafeExperimentLedgerV2,
    kind: str,
    payload: dict[str, object],
    *,
    identity: tuple[str, object],
) -> None:
    existing = [
        event["payload"]
        for event in ledger.events
        if event["kind"] == kind and event["payload"].get(identity[0]) == identity[1]
    ]
    if existing:
        if existing != [payload]:
            raise FoldRunError("SAFE_PROTOCOL_RECEIPT_DRIFT")
        return
    ledger.append(kind, payload)


def _persist_exact(path: Path, payload: bytes) -> None:
    if path.exists():
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or path.read_bytes() != payload
        ):
            raise FoldRunError("SAFE_FOLD_IMMUTABLE_EVIDENCE_DRIFT")
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    write_private_file(path, payload, replace=False)


def _fresh_private_directory(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    if path.exists() or path.is_symlink():
        raise FoldRunError("SAFE_FOLD_FRESH_ROOT_EXISTS")
    path.mkdir(mode=0o700)


def _require_private_directory(path: Path) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.geteuid()
    ):
        raise FoldRunError("SAFE_FOLD_PRIVATE_ROOT_INVALID")


__all__ = [
    "FoldRunError",
    "FoldRunOutcomeV2",
    "LogicalArmResultV2",
    "SafeFoldRunnerV2",
]
