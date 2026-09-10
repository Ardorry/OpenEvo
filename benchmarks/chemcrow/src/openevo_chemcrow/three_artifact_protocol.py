from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Protocol

from .cache import assert_real_metric_observations
from .compatible_evaluation import confidence_parse_receipt
from .feedback import feedback_hash, reflector_feedback_payload, runtime_feedback
from .hashing import canonical_sha256, file_sha256
from .models import (
    ArtifactKind,
    EvaluatorFeedback,
    FeedbackMode,
    PairwiseEvaluation,
    RuntimeFeedback,
    TaskItem,
    Trajectory,
)
from .protocol import BlindJudgeResult, EvolutionEvaluatorPort, FinalEvaluatorPort
from .replacement_ledger import VerifiedReplacementPhaseLedger
from .three_artifact_evolution import ThreeIsolatedEvolutionEngine
from .three_artifact_models import (
    THREE_ARTIFACT_ORDER,
    RecoveredReflectorOutput,
    ThreeArtifactBundleReceipt,
    ThreeArtifactGenerationResult,
    ThreeArtifactPairResult,
    three_artifact_protocol_label,
)
from .three_artifact_runtime import ThreeArtifactRolloutPort


def _task_authority_sha256(task: TaskItem) -> str:
    payload = task.model_dump(mode="json")
    body = dict(payload)
    stored_sha256 = str(body.pop("sanitized_item_sha256"))
    if canonical_sha256(body) != stored_sha256:
        raise ValueError("task sanitized-item hash differs from the full canonical body")
    return canonical_sha256(payload)


class ThreeArtifactCandidatePort(Protocol):
    config_sha256: str

    def run_candidate_with_receipt(
        self,
        *,
        task: TaskItem,
        role: str,
        artifact_ids_by_type: dict[ArtifactKind, str],
        pair_id: str,
        mcp_url: str | None,
        run_id: str | None = None,
    ): ...


class ThreeArtifactEvolutionPort(Protocol):
    def evolve_all(
        self,
        *,
        task: TaskItem,
        baseline: Trajectory,
        feedback_payload: dict,
        pair_id: str,
        recovered_outputs: dict[ArtifactKind, RecoveredReflectorOutput] | None = None,
    ) -> ThreeArtifactBundleReceipt: ...


class ThreeArtifactTaskLocalProtocolRunner:
    def __init__(
        self,
        *,
        run_root: Path,
        candidate: ThreeArtifactRolloutPort,
        evolution: ThreeIsolatedEvolutionEngine,
        evolution_evaluator: EvolutionEvaluatorPort,
        final_evaluator: FinalEvaluatorPort,
        feedback_mode: FeedbackMode,
        s0_hash: str,
        real_mode: bool,
        random_seed: int = 20260825,
        ledger_root: Path | None = None,
        stop_after_artifact_generation: bool = False,
    ) -> None:
        if evolution_evaluator.evaluator_id == final_evaluator.evaluator_id:
            raise ValueError(
                "evolution_evaluator and final_evaluator must be independently configured"
            )
        self.run_root = run_root
        self.candidate = candidate
        self.evolution = evolution
        self.evolution_evaluator = evolution_evaluator
        self.final_evaluator = final_evaluator
        self.feedback_mode = feedback_mode
        self.s0_hash = s0_hash
        self.real_mode = real_mode
        self.random_seed = random_seed
        self.ledger_root = ledger_root
        self.stop_after_artifact_generation = stop_after_artifact_generation
        self._active_artifacts: dict[ArtifactKind, tuple[str, str]] = {}
        self._seen_artifact_ids: set[str] = set()
        self._observed_s0_hashes: set[str] = set()

    def run_item(
        self,
        task: TaskItem,
        *,
        pair_id: str,
        mcp_url: str | None = None,
        allow_verified_baseline_replacement: bool = False,
        recovered_baseline_before_evaluator: Trajectory | None = None,
        allow_verified_baseline_evaluator_replacement: bool = False,
        recovered_baseline_checkpoint: tuple[Trajectory, EvaluatorFeedback] | None = None,
        allow_verified_reflector_replacements: frozenset[str] = frozenset(),
        recovered_reflector_outputs: dict[
            ArtifactKind, RecoveredReflectorOutput
        ] | None = None,
        recovered_artifact_checkpoint: ThreeArtifactBundleReceipt | None = None,
        continue_from_sealed_generation: bool = False,
        allow_verified_evolved_candidate_replacement: bool = False,
        replacement_evolved_run_id: str | None = None,
    ) -> ThreeArtifactPairResult | ThreeArtifactGenerationResult:
        self._assert_bare_s0(task=task)
        recovered_reflector_outputs = dict(recovered_reflector_outputs or {})
        if recovered_reflector_outputs and recovered_baseline_checkpoint is None:
            raise ValueError("recovered Reflector outputs require recovered G1")
        if {
            _reflector_phase(kind) for kind in recovered_reflector_outputs
        } & set(allow_verified_reflector_replacements):
            raise ValueError("completed and failed Reflector recovery phases overlap")
        if recovered_baseline_before_evaluator is not None and (
            recovered_baseline_checkpoint is not None
            or allow_verified_baseline_replacement
            or not allow_verified_baseline_evaluator_replacement
        ):
            raise ValueError("baseline evaluator replacement boundary is inconsistent")
        if allow_verified_baseline_evaluator_replacement and (
            recovered_baseline_before_evaluator is None
        ):
            raise ValueError("baseline evaluator replacement requires recovered G1")
        if continue_from_sealed_generation:
            if (
                recovered_artifact_checkpoint is None
                or recovered_baseline_checkpoint is None
                or allow_verified_evolved_candidate_replacement
                or replacement_evolved_run_id is not None
                or self.stop_after_artifact_generation
            ):
                raise ValueError("sealed generation continuation boundary is inconsistent")
        elif (recovered_artifact_checkpoint is None) != (
            not allow_verified_evolved_candidate_replacement
        ):
            raise ValueError(
                "evolved Candidate replacement requires one recovered artifact checkpoint"
            )
        if recovered_artifact_checkpoint is not None and (
            recovered_baseline_checkpoint is None or allow_verified_reflector_replacements
        ):
            raise ValueError(
                "recovered artifact checkpoint requires the exact recovered G1 boundary"
            )
        if not continue_from_sealed_generation and (replacement_evolved_run_id is None) != (
            not allow_verified_evolved_candidate_replacement
        ):
            raise ValueError("evolved Candidate replacement requires one preallocated run ID")
        if recovered_artifact_checkpoint is not None and self.ledger_root is None:
            raise ValueError("recovered artifacts require a verified replacement ledger")
        ledger = (
            VerifiedReplacementPhaseLedger(self.ledger_root, pair_id=pair_id)
            if self.ledger_root
            else None
        )
        evolved_replacement_expectation = None
        if allow_verified_evolved_candidate_replacement and ledger is not None:
            evolved_replacement_expectation = ledger.replacement_expectation("evolved_candidate")
            recovery_authority = ledger.evolved_replacement_receipt()
            if (
                recovery_authority["replacement_run_id"] != replacement_evolved_run_id
                or evolved_replacement_expectation.replacement_run_id != replacement_evolved_run_id
                or recovery_authority["task_authority_sha256"] != _task_authority_sha256(task)
                or recovery_authority["artifact_ids_by_type"]
                != recovered_artifact_checkpoint.artifact_id_by_type()
                or recovery_authority["input_evidence_sha256"]
                != recovered_artifact_checkpoint.input_evidence_hash
                or recovery_authority["reflector_job_ids"]
                != [
                    receipt.reflector_job_id for receipt in recovered_artifact_checkpoint.artifacts
                ]
                or recovery_authority["reflector_run_ids"]
                != [
                    receipt.reflector_run_id for receipt in recovered_artifact_checkpoint.artifacts
                ]
                or recovery_authority["reflector_prompt_hashes"]
                != [receipt.prompt_hash for receipt in recovered_artifact_checkpoint.artifacts]
            ):
                raise ValueError("replacement G2 differs from verified ledger authority")
        event_order = ["s0_asserted"]
        if recovered_baseline_checkpoint is not None:
            if allow_verified_baseline_replacement:
                raise ValueError("baseline replacement and checkpoint recovery cannot be combined")
            baseline, baseline_evaluation = recovered_baseline_checkpoint
            if (
                baseline.task_id != task.task_id
                or baseline.role != "baseline"
                or baseline.status != "COMPLETED"
                or not baseline.answer.strip()
                or baseline.artifact_ids
                or baseline.candidate_config_sha256 != self.candidate.config_sha256
            ):
                raise ValueError("recovered baseline checkpoint authority is invalid")
            _assert_internal_evaluation(baseline_evaluation)
            if baseline_evaluation.evaluator_role != "evolution_evaluator":
                raise ValueError("recovered baseline evaluator role is invalid")
            event_order.extend(["baseline", "runtime_feedback", "baseline_internal_evaluator"])
        elif recovered_baseline_before_evaluator is not None:
            baseline = recovered_baseline_before_evaluator
            if (
                baseline.task_id != task.task_id
                or baseline.role != "baseline"
                or baseline.status != "COMPLETED"
                or not baseline.answer.strip()
                or baseline.artifact_ids
                or baseline.candidate_config_sha256 != self.candidate.config_sha256
            ):
                raise ValueError("recovered pre-evaluator baseline authority is invalid")
            if ledger is None:
                raise ValueError("baseline evaluator replacement requires verified ledger")
            evaluator_expectation = ledger.replacement_expectation(
                "baseline_internal_evaluator"
            )
            evaluator_activation = ledger.claim(
                "baseline_internal_evaluator",
                {
                    "task_id": task.task_id,
                    "run_id": baseline.run_id,
                    "evaluator_id": self.evolution_evaluator.evaluator_id,
                    "paper_evaluator": False,
                },
                allow_verified_replacement=True,
                expected_replacement=evaluator_expectation,
            )
            event_order.extend(["baseline", "runtime_feedback"])
            baseline_evaluation = self.evolution_evaluator.evaluate(
                task=task, trajectory=baseline
            )
            _assert_internal_evaluation(baseline_evaluation)
            parse_receipt = confidence_parse_receipt(self.evolution_evaluator)
            ledger.terminal(
                "baseline_internal_evaluator",
                {
                    "evaluator_run_id": baseline_evaluation.evaluator_run_id,
                    "feedback_sha256": canonical_sha256(
                        baseline_evaluation.model_dump(mode="json")
                    ),
                    **(
                        {"confidence_parse_receipt": parse_receipt}
                        if parse_receipt is not None
                        else {}
                    ),
                },
                replacement_activation=evaluator_activation,
            )
            event_order.append("baseline_internal_evaluator")
        else:
            baseline_replacement_activation = None
            baseline_replacement_expectation = None
            if ledger:
                if allow_verified_baseline_replacement:
                    baseline_replacement_expectation = ledger.replacement_expectation(
                        "baseline_candidate"
                    )
                baseline_replacement_activation = ledger.claim(
                    "baseline_candidate",
                    {
                        "task_id": task.task_id,
                        "s0_hash": self.s0_hash,
                        "artifact_ids": [],
                        "artifact_inventory": {},
                    },
                    allow_verified_replacement=allow_verified_baseline_replacement,
                    expected_replacement=baseline_replacement_expectation,
                )
            baseline, baseline_injection = self.candidate.run_candidate_with_receipt(
                task=task,
                role="baseline",
                artifact_ids_by_type={},
                pair_id=pair_id,
                mcp_url=mcp_url,
            )
            if baseline_injection is not None:
                raise ValueError("baseline received a runtime injection receipt")
            if (
                baseline.task_id != task.task_id
                or baseline.role != "baseline"
                or baseline.artifact_ids
            ):
                raise ValueError("baseline authority is invalid")
            if baseline.status != "COMPLETED" or not baseline.answer.strip():
                raise ValueError("baseline Candidate did not complete with a final answer")
            if baseline.candidate_config_sha256 != self.candidate.config_sha256:
                raise ValueError("baseline is not bound to frozen S0 configuration")
            if ledger:
                ledger.terminal(
                    "baseline_candidate",
                    {
                        "run_id": baseline.run_id,
                        "trajectory_sha256": canonical_sha256(baseline.model_dump(mode="json")),
                        "runtime_context": "bare_s0",
                    },
                    replacement_activation=baseline_replacement_activation,
                )
            event_order.append("baseline")
            runtime = runtime_feedback(baseline)
            event_order.append("runtime_feedback")
            if ledger:
                ledger.claim(
                    "baseline_internal_evaluator",
                    {
                        "task_id": task.task_id,
                        "run_id": baseline.run_id,
                        "evaluator_id": self.evolution_evaluator.evaluator_id,
                        "paper_evaluator": False,
                    },
                )
            baseline_evaluation = self.evolution_evaluator.evaluate(task=task, trajectory=baseline)
            _assert_internal_evaluation(baseline_evaluation)
            if ledger:
                parse_receipt = confidence_parse_receipt(self.evolution_evaluator)
                ledger.terminal(
                    "baseline_internal_evaluator",
                    {
                        "evaluator_run_id": baseline_evaluation.evaluator_run_id,
                        "feedback_sha256": canonical_sha256(
                            baseline_evaluation.model_dump(mode="json")
                        ),
                        **(
                            {"confidence_parse_receipt": parse_receipt}
                            if parse_receipt is not None
                            else {}
                        ),
                    },
                )
            event_order.append("baseline_internal_evaluator")

        runtime = runtime_feedback(baseline)

        feedback_payload = reflector_feedback_payload(
            mode=self.feedback_mode,
            trajectory=baseline,
            runtime=runtime,
            evaluator=baseline_evaluation,
        )
        frozen_feedback_hash = feedback_hash(feedback_payload)
        frozen_input_hash = canonical_sha256(
            {
                "task": {
                    "task_id": task.task_id,
                    "prompt": task.prompt,
                    "sanitized_item_sha256": task.sanitized_item_sha256,
                },
                "baseline": baseline.model_dump(mode="json"),
                "feedback": feedback_payload,
            }
        )
        reflector_replacement_activations = {}
        if recovered_artifact_checkpoint is None:
            for kind in THREE_ARTIFACT_ORDER:
                phase = _reflector_phase(kind)
                if ledger and kind not in recovered_reflector_outputs:
                    reflector_expectation = (
                        ledger.replacement_expectation(phase)
                        if phase in allow_verified_reflector_replacements
                        else None
                    )
                    reflector_replacement_activations[phase] = ledger.claim(
                        phase,
                        {
                            "task_id": task.task_id,
                            "pair_id": pair_id,
                            "parent_run_id": baseline.run_id,
                            "artifact_type": kind.value,
                            "input_evidence_hash": frozen_input_hash,
                            "sibling_artifact_ids": [],
                            "paper_evaluator_feedback_included": False,
                        },
                        allow_verified_replacement=(
                            phase in allow_verified_reflector_replacements
                        ),
                        expected_replacement=reflector_expectation,
                    )
            evolve_kwargs = {
                "task": task,
                "baseline": baseline,
                "feedback_payload": feedback_payload,
                "pair_id": pair_id,
            }
            if recovered_reflector_outputs:
                evolve_kwargs["recovered_outputs"] = recovered_reflector_outputs
            bundle = self.evolution.evolve_all(**evolve_kwargs)
        else:
            bundle = recovered_artifact_checkpoint
            if (
                bundle.task_id != task.task_id
                or bundle.pair_id != pair_id
                or bundle.parent_run_id != baseline.run_id
                or any(
                    receipt.consumed_by_evolved_run_id is not None for receipt in bundle.artifacts
                )
            ):
                raise ValueError("recovered artifact checkpoint authority is invalid")
        if bundle.input_evidence_hash != frozen_input_hash:
            raise ValueError("Reflector evidence differs from the pre-G2 frozen authority")
        for receipt in bundle.artifacts:
            phase = _reflector_phase(receipt.artifact_type)
            if ledger and recovered_artifact_checkpoint is None:
                ledger.terminal(
                    phase,
                    {
                        "reflector_job_id": receipt.reflector_job_id,
                        "reflector_run_id": receipt.reflector_run_id,
                        "model": receipt.model,
                        "prompt_hash": receipt.prompt_hash,
                        "input_evidence_hash": receipt.input_evidence_hash,
                        "artifact_id": receipt.artifact_id,
                        "artifact_hash": receipt.artifact_hash,
                        "artifact_type": receipt.artifact_type.value,
                        "registration_receipt_sha256": receipt.registration_receipt_sha256,
                    },
                    replacement_activation=reflector_replacement_activations.get(phase),
                )
            event_order.append(phase)
            if receipt.artifact_id in self._seen_artifact_ids:
                raise ValueError("artifact ID was reused across tasks or types")
            self._seen_artifact_ids.add(receipt.artifact_id)
            self._active_artifacts[receipt.artifact_type] = (
                task.task_id,
                receipt.artifact_id,
            )
        if set(self._active_artifacts) != set(THREE_ARTIFACT_ORDER):
            raise ValueError("task-local artifact inventory is incomplete")
        event_order.append("artifacts_registered")

        if self.stop_after_artifact_generation:
            return self._seal_generation_only(
                task=task,
                pair_id=pair_id,
                baseline=baseline,
                runtime=runtime,
                baseline_evaluation=baseline_evaluation,
                feedback_payload=feedback_payload,
                frozen_feedback_hash=frozen_feedback_hash,
                bundle=bundle,
                event_order=event_order,
            )

        artifact_mapping = {
            receipt.artifact_type: receipt.artifact_id for receipt in bundle.artifacts
        }
        evolved_replacement_activation = None
        if ledger:
            evolved_replacement_activation = ledger.claim(
                "evolved_candidate",
                {
                    "task_id": task.task_id,
                    "s0_hash": self.s0_hash,
                    "artifact_ids_by_type": {
                        kind.value: artifact_mapping[kind] for kind in THREE_ARTIFACT_ORDER
                    },
                    "artifact_count": 3,
                },
                allow_verified_replacement=(allow_verified_evolved_candidate_replacement),
                expected_replacement=evolved_replacement_expectation,
            )
        evolved, injection = self.candidate.run_candidate_with_receipt(
            task=task,
            role="evolved",
            artifact_ids_by_type=artifact_mapping,
            pair_id=pair_id,
            mcp_url=mcp_url,
            run_id=replacement_evolved_run_id,
        )
        if injection is None:
            raise ValueError("G2 did not return a three-artifact injection receipt")
        if evolved.task_id != task.task_id or evolved.role != "evolved":
            raise ValueError("evolved authority is invalid")
        if evolved.status != "COMPLETED" or not evolved.answer.strip():
            raise ValueError("evolved Candidate did not complete with a final answer")
        if baseline.candidate_config_sha256 != evolved.candidate_config_sha256:
            raise ValueError("baseline/evolved candidate configuration parity failed")
        if evolved.artifact_ids != bundle.artifact_ids():
            raise ValueError("G2 artifact inventory differs from the registered bundle")
        if ledger:
            ledger.terminal(
                "evolved_candidate",
                {
                    "run_id": evolved.run_id,
                    "trajectory_sha256": canonical_sha256(evolved.model_dump(mode="json")),
                    "runtime_injection_receipt_sha256": injection.receipt_sha256,
                    "artifact_ids_by_type": bundle.artifact_id_by_type(),
                    "artifact_count": 3,
                },
                replacement_activation=evolved_replacement_activation,
            )
        event_order.append("evolved")
        bundle = bundle.model_copy(
            update={
                "artifacts": [
                    item.model_copy(update={"consumed_by_evolved_run_id": evolved.run_id})
                    for item in bundle.artifacts
                ]
            }
        )

        # Same evaluator instance/config as G1. This result is sealed after G2
        # and cannot flow backward because all Reflector prompts are already run.
        if ledger:
            ledger.claim(
                "evolved_internal_evaluator",
                {
                    "task_id": task.task_id,
                    "run_id": evolved.run_id,
                    "evaluator_id": self.evolution_evaluator.evaluator_id,
                    "paper_evaluator": False,
                    "reflectors_already_terminal": True,
                },
            )
        evolved_evaluation = self.evolution_evaluator.evaluate(task=task, trajectory=evolved)
        _assert_internal_evaluation(evolved_evaluation)
        if ledger:
            parse_receipt = confidence_parse_receipt(self.evolution_evaluator)
            ledger.terminal(
                "evolved_internal_evaluator",
                {
                    "evaluator_run_id": evolved_evaluation.evaluator_run_id,
                    "feedback_sha256": canonical_sha256(
                        evolved_evaluation.model_dump(mode="json")
                    ),
                    **(
                        {"confidence_parse_receipt": parse_receipt}
                        if parse_receipt is not None
                        else {}
                    ),
                },
            )
        event_order.append("evolved_internal_evaluator")
        if feedback_hash(feedback_payload) != frozen_feedback_hash:
            raise ValueError("Reflector feedback mutated after G2")
        if evolved.run_id in json.dumps(feedback_payload, sort_keys=True):
            raise ValueError("future G2 evidence leaked backward into Reflector input")

        if ledger:
            ledger.claim(
                "final_evaluator",
                {
                    "task_id": task.task_id,
                    "baseline_run_id": baseline.run_id,
                    "evolved_run_id": evolved.run_id,
                    "evaluator_id": self.final_evaluator.evaluator_id,
                    "paper_evaluator": False,
                },
            )
        final = self._blind_compare(
            task=task,
            baseline=baseline,
            evolved=evolved,
            pair_id=pair_id,
        )
        if ledger:
            parse_receipt = confidence_parse_receipt(self.final_evaluator)
            ledger.terminal(
                "final_evaluator",
                {
                    "mapping_seal_sha256": final.mapping_seal_sha256,
                    "winner": final.winner,
                    "pair_scores_sha256": canonical_sha256(final.model_dump(mode="json")),
                    **(
                        {"confidence_parse_receipt": parse_receipt}
                        if parse_receipt is not None
                        else {}
                    ),
                },
            )
        event_order.append("final_evaluator")
        if self.real_mode:
            assert_real_metric_observations(baseline.observations + evolved.observations)

        item_root = self.run_root / pair_id
        item_root.mkdir(parents=True, exist_ok=True)
        baseline_path = item_root / "baseline.trajectory.json"
        evolved_path = item_root / "evolved.trajectory.json"
        feedback_path = item_root / "feedback.json"
        artifacts_path = item_root / "artifacts.receipt.json"
        injection_path = item_root / "injection.receipt.summary.json"
        baseline_path.write_text(baseline.model_dump_json(indent=2), encoding="utf-8")
        evolved_path.write_text(evolved.model_dump_json(indent=2), encoding="utf-8")
        feedback_path.write_text(
            json.dumps(feedback_payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        artifacts_path.write_text(bundle.model_dump_json(indent=2), encoding="utf-8")
        injection_path.write_text(injection.model_dump_json(indent=2), encoding="utf-8")
        event_order.append("sealed")

        discarded = bundle.artifact_id_by_type()
        self._active_artifacts.clear()
        reset_payload = {
            "schema_version": "chemcrow_three_artifact_reset_v1",
            "task_id": task.task_id,
            "pair_id": pair_id,
            "discarded_task_local_artifact_ids": discarded,
            "active_artifact_ids_after": [],
            "artifact_inventory_after": [],
            "prior_artifact_ids_exported": [],
            "memory_after": [],
            "skill_bundle_after": [],
            "agent_system_after": [],
            "runtime_context_after": "bare_s0",
            "s0_hash_for_next_item": self.s0_hash,
            "archival_evidence_retained": True,
            "archival_artifacts_auto_selected": False,
        }
        reset_path = item_root / "reset.receipt.json"
        reset_path.write_text(
            json.dumps(reset_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        event_order.append("reset")

        result = ThreeArtifactPairResult(
            task_id=task.task_id,
            task_category=task.broad_category,
            pair_id=pair_id,
            s0_hash=self.s0_hash,
            task_prompt_hash=canonical_sha256({"task_prompt": task.prompt}),
            baseline=baseline,
            runtime_feedback=runtime,
            baseline_internal_evaluation=baseline_evaluation,
            feedback_hash=frozen_feedback_hash,
            artifact_bundle=bundle,
            evolved=evolved,
            evolved_internal_evaluation=evolved_evaluation,
            injection_receipt=injection,
            final_evaluation=final,
            event_order=event_order,
            reset_receipt_sha256=file_sha256(reset_path),
        )
        (item_root / "pair.result.json").write_text(
            result.model_dump_json(indent=2), encoding="utf-8"
        )
        self._write_human_packet(
            task=task,
            pair_id=pair_id,
            baseline=baseline,
            evolved=evolved,
            mapping_seal=final.mapping_seal_sha256,
            artifact_protocol=three_artifact_protocol_label(bundle.protocol),
        )
        return result

    def _seal_generation_only(
        self,
        *,
        task: TaskItem,
        pair_id: str,
        baseline: Trajectory,
        runtime: RuntimeFeedback,
        baseline_evaluation: EvaluatorFeedback,
        feedback_payload: dict,
        frozen_feedback_hash: str,
        bundle: ThreeArtifactBundleReceipt,
        event_order: list[str],
    ) -> ThreeArtifactGenerationResult:
        if self.real_mode:
            assert_real_metric_observations(baseline.observations)
        item_root = self.run_root / pair_id
        item_root.mkdir(parents=True, exist_ok=True)
        baseline_path = item_root / "baseline.trajectory.json"
        feedback_path = item_root / "feedback.json"
        artifacts_path = item_root / "artifacts.receipt.json"
        baseline_path.write_text(baseline.model_dump_json(indent=2), encoding="utf-8")
        feedback_path.write_text(
            json.dumps(feedback_payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        artifacts_path.write_text(bundle.model_dump_json(indent=2), encoding="utf-8")
        boundary_payload = {
            "schema_version": "chemcrow_g1_artifact_generation_boundary_v1",
            "status": "G1_AND_ARTIFACTS_SEALED",
            "task_id": task.task_id,
            "pair_id": pair_id,
            "baseline_trajectory_sha256": file_sha256(baseline_path),
            "feedback_sha256": file_sha256(feedback_path),
            "artifact_bundle_sha256": file_sha256(artifacts_path),
            "artifact_ids_by_type": bundle.artifact_id_by_type(),
            "g2_dispatched": False,
            "g2_evaluator_dispatched": False,
            "final_evaluator_dispatched": False,
        }
        (item_root / "generation.boundary.receipt.json").write_text(
            json.dumps(boundary_payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        event_order.append("sealed")

        discarded = bundle.artifact_id_by_type()
        self._active_artifacts.clear()
        reset_payload = {
            "schema_version": "chemcrow_three_artifact_generation_reset_v1",
            "task_id": task.task_id,
            "pair_id": pair_id,
            "discarded_task_local_artifact_ids": discarded,
            "active_artifact_ids_after": [],
            "artifact_inventory_after": [],
            "prior_artifact_ids_exported": [],
            "memory_after": [],
            "skill_bundle_after": [],
            "agent_system_after": [],
            "runtime_context_after": "bare_s0",
            "s0_hash_for_next_item": self.s0_hash,
            "archival_evidence_retained": True,
            "archival_artifacts_auto_selected": False,
            "g2_dispatched": False,
        }
        reset_path = item_root / "reset.receipt.json"
        reset_path.write_text(
            json.dumps(reset_payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        event_order.append("reset")
        result = ThreeArtifactGenerationResult(
            task_id=task.task_id,
            task_category=task.broad_category,
            pair_id=pair_id,
            s0_hash=self.s0_hash,
            task_prompt_hash=canonical_sha256({"task_prompt": task.prompt}),
            baseline=baseline,
            runtime_feedback=runtime,
            baseline_internal_evaluation=baseline_evaluation,
            feedback_hash=frozen_feedback_hash,
            artifact_bundle=bundle,
            event_order=event_order,
            reset_receipt_sha256=file_sha256(reset_path),
        )
        (item_root / "artifact.study.result.json").write_text(
            result.model_dump_json(indent=2),
            encoding="utf-8",
        )
        return result

    def _assert_bare_s0(self, *, task: TaskItem) -> None:
        if self._active_artifacts:
            raise ValueError("S0 reset failed: prior task-local artifacts remain active")
        if self._observed_s0_hashes and self._observed_s0_hashes != {self.s0_hash}:
            raise ValueError("S0 hash drift was detected before item start")
        self._observed_s0_hashes.add(self.s0_hash)
        if any(task.task_id == owner for owner, _ in self._active_artifacts.values()):
            raise ValueError("task baseline inherited prior task artifacts")

    def _blind_compare(
        self,
        *,
        task: TaskItem,
        baseline: Trajectory,
        evolved: Trajectory,
        pair_id: str,
    ) -> PairwiseEvaluation:
        rng = random.Random(f"{self.random_seed}:{pair_id}")
        baseline_is_a = bool(rng.getrandbits(1))
        answer_a = baseline.answer if baseline_is_a else evolved.answer
        answer_b = evolved.answer if baseline_is_a else baseline.answer
        mapping = {
            "A": "baseline" if baseline_is_a else "evolved",
            "B": "evolved" if baseline_is_a else "baseline",
        }
        mapping_seal = canonical_sha256({"pair_id": pair_id, "mapping": mapping})
        judged: BlindJudgeResult = self.final_evaluator.compare(
            task=task,
            answer_a=answer_a,
            answer_b=answer_b,
        )
        if judged.winner not in {"A", "B", "tie"}:
            raise ValueError("final evaluator winner is invalid")
        baseline_scores = judged.scores_a if baseline_is_a else judged.scores_b
        evolved_scores = judged.scores_b if baseline_is_a else judged.scores_a
        winner = "tie" if judged.winner == "tie" else mapping[judged.winner]
        return PairwiseEvaluation(
            baseline_scores=baseline_scores,
            evolved_scores=evolved_scores,
            delta_chemical_correctness=(
                evolved_scores.chemical_correctness - baseline_scores.chemical_correctness
            ),
            delta_reasoning_quality=(
                evolved_scores.reasoning_quality - baseline_scores.reasoning_quality
            ),
            delta_task_completion=(
                evolved_scores.task_completion - baseline_scores.task_completion
            ),
            winner=winner,
            confidence=judged.confidence,
            presentation_order=["A", "B"],
            mapping_seal_sha256=mapping_seal,
            provisional_llm_judged=True,
        )

    def _write_human_packet(
        self,
        *,
        task: TaskItem,
        pair_id: str,
        baseline: Trajectory,
        evolved: Trajectory,
        mapping_seal: str,
        artifact_protocol: str,
    ) -> None:
        rng = random.Random(f"{self.random_seed}:{pair_id}")
        baseline_is_a = bool(rng.getrandbits(1))
        packet = {
            "schema_version": "chemcrow_human_review_packet_v2_three_artifact",
            "pair_id": pair_id,
            "task_id": task.task_id,
            "task_category": task.broad_category,
            "task_prompt": task.prompt,
            "A": baseline.answer if baseline_is_a else evolved.answer,
            "B": evolved.answer if baseline_is_a else baseline.answer,
            "rubric": ["chemical correctness", "reasoning quality", "task completion"],
            "mapping_seal_sha256": mapping_seal,
            "historical_answers_included": False,
            "artifact_protocol": artifact_protocol,
        }
        review_dir = self.run_root / "human_review"
        review_dir.mkdir(parents=True, exist_ok=True)
        (review_dir / f"{pair_id}.blinded.json").write_text(
            json.dumps(packet, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )


def _reflector_phase(kind: ArtifactKind) -> str:
    return {
        ArtifactKind.TEXT_MEMORY: "reflector_memory",
        ArtifactKind.SKILL_BUNDLE: "reflector_skill_bundle",
        ArtifactKind.AGENT_SYSTEM: "reflector_agent_system",
    }[kind]


def _assert_internal_evaluation(value: EvaluatorFeedback) -> None:
    if value.evaluator_role != "evolution_evaluator":
        raise ValueError("internal evaluator returned the wrong role")
