from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .cache import assert_real_metric_observations
from .feedback import feedback_hash, reflector_feedback_payload, runtime_feedback
from .hashing import canonical_sha256, file_sha256
from .ledger import PhaseLedger
from .models import (
    ArtifactReceipt,
    EvaluatorFeedback,
    FeedbackMode,
    PairResult,
    PairwiseEvaluation,
    RubricScores,
    TaskItem,
    Trajectory,
)


class CandidatePort(Protocol):
    config_sha256: str

    def run_candidate(
        self,
        *,
        task: TaskItem,
        role: str,
        artifact_ids: list[str],
        pair_id: str,
        mcp_url: str | None,
    ) -> Trajectory: ...


class EvolutionPort(Protocol):
    def evolve(
        self,
        *,
        task: TaskItem,
        baseline: Trajectory,
        feedback_payload: dict[str, Any],
        pair_id: str,
    ) -> ArtifactReceipt: ...


class EvolutionEvaluatorPort(Protocol):
    evaluator_id: str

    def evaluate(self, *, task: TaskItem, trajectory: Trajectory) -> EvaluatorFeedback: ...


@dataclass(frozen=True)
class BlindJudgeResult:
    scores_a: RubricScores
    scores_b: RubricScores
    winner: str
    confidence: float | None = None


class FinalEvaluatorPort(Protocol):
    evaluator_id: str

    def compare(
        self,
        *,
        task: TaskItem,
        answer_a: str,
        answer_b: str,
    ) -> BlindJudgeResult: ...


class TaskLocalProtocolRunner:
    def __init__(
        self,
        *,
        run_root: Path,
        candidate: CandidatePort,
        evolution: EvolutionPort,
        evolution_evaluator: EvolutionEvaluatorPort,
        final_evaluator: FinalEvaluatorPort,
        feedback_mode: FeedbackMode,
        s0_hash: str,
        real_mode: bool,
        random_seed: int = 20260825,
        ledger_root: Path | None = None,
    ) -> None:
        if evolution_evaluator.evaluator_id == final_evaluator.evaluator_id:
            raise ValueError("evolution_evaluator and final_evaluator must be independently configured")
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
        self._active_artifact: tuple[str, str] | None = None
        self._seen_artifact_ids: set[str] = set()
        self._observed_s0_hashes: set[str] = set()

    def run_item(self, task: TaskItem, *, pair_id: str, mcp_url: str | None = None) -> PairResult:
        if self._active_artifact is not None:
            raise ValueError("S0 reset failed: a previous task artifact remains active")
        if self._observed_s0_hashes and self._observed_s0_hashes != {self.s0_hash}:
            raise ValueError("S0 hash drift was detected before item start")
        self._observed_s0_hashes.add(self.s0_hash)
        ledger = PhaseLedger(self.ledger_root, pair_id=pair_id) if self.ledger_root else None
        event_order = ["s0_asserted"]
        if ledger:
            ledger.claim(
                "baseline_candidate",
                {"task_id": task.task_id, "s0_hash": self.s0_hash, "artifact_ids": []},
            )
        baseline = self.candidate.run_candidate(
            task=task,
            role="baseline",
            artifact_ids=[],
            pair_id=pair_id,
            mcp_url=mcp_url,
        )
        if ledger:
            ledger.terminal(
                "baseline_candidate",
                {"run_id": baseline.run_id, "trajectory_sha256": canonical_sha256(baseline.model_dump(mode="json"))},
            )
        event_order.append("baseline")
        if baseline.task_id != task.task_id or baseline.role != "baseline" or baseline.artifact_ids:
            raise ValueError("baseline authority is invalid")
        runtime = runtime_feedback(baseline)
        event_order.append("runtime_feedback")
        if ledger:
            ledger.claim(
                "evolution_evaluator",
                {"task_id": task.task_id, "baseline_run_id": baseline.run_id, "evaluator_id": self.evolution_evaluator.evaluator_id},
            )
        evaluator_feedback = self.evolution_evaluator.evaluate(task=task, trajectory=baseline)
        if ledger:
            ledger.terminal(
                "evolution_evaluator",
                {"evaluator_run_id": evaluator_feedback.evaluator_run_id, "feedback_sha256": canonical_sha256(evaluator_feedback.model_dump(mode="json"))},
            )
        if evaluator_feedback.evaluator_role != "evolution_evaluator":
            raise ValueError("evolution evaluator returned the wrong role")
        event_order.append("evolution_evaluator")
        feedback_payload = reflector_feedback_payload(
            mode=self.feedback_mode,
            trajectory=baseline,
            runtime=runtime,
            evaluator=evaluator_feedback,
        )
        if ledger:
            ledger.claim(
                "reflector",
                {"task_id": task.task_id, "baseline_run_id": baseline.run_id, "reflector_input_sha256": canonical_sha256(feedback_payload)},
            )
        artifact = self.evolution.evolve(
            task=task,
            baseline=baseline,
            feedback_payload=feedback_payload,
            pair_id=pair_id,
        )
        if ledger:
            ledger.terminal(
                "reflector",
                {"reflector_run_id": artifact.reflector_run_id, "artifact_id": artifact.artifact_id, "artifact_hash": artifact.content_hash},
            )
        event_order.append("reflector")
        if artifact.task_id != task.task_id or artifact.parent_baseline_run != baseline.run_id:
            raise ValueError("Reflector artifact lineage differs from current item")
        if artifact.artifact_id in self._seen_artifact_ids:
            raise ValueError("artifact ID was reused across items")
        self._active_artifact = (task.task_id, artifact.artifact_id)
        self._seen_artifact_ids.add(artifact.artifact_id)
        if ledger:
            ledger.claim(
                "evolved_candidate",
                {"task_id": task.task_id, "s0_hash": self.s0_hash, "artifact_ids": [artifact.artifact_id]},
            )
        evolved = self.candidate.run_candidate(
            task=task,
            role="evolved",
            artifact_ids=[artifact.artifact_id],
            pair_id=pair_id,
            mcp_url=mcp_url,
        )
        if ledger:
            ledger.terminal(
                "evolved_candidate",
                {"run_id": evolved.run_id, "trajectory_sha256": canonical_sha256(evolved.model_dump(mode="json"))},
            )
        event_order.append("evolved")
        if evolved.task_id != task.task_id or evolved.role != "evolved":
            raise ValueError("evolved authority is invalid")
        if baseline.candidate_config_sha256 != evolved.candidate_config_sha256:
            raise ValueError("baseline/evolved candidate configuration parity failed")
        if baseline.candidate_config_sha256 != self.candidate.config_sha256:
            raise ValueError("candidate trajectory is not bound to frozen S0 configuration")
        artifact = artifact.model_copy(update={"consumed_by_evolved_run_id": evolved.run_id})
        if ledger:
            ledger.claim(
                "final_evaluator",
                {"task_id": task.task_id, "baseline_run_id": baseline.run_id, "evolved_run_id": evolved.run_id, "evaluator_id": self.final_evaluator.evaluator_id},
            )
        final = self._blind_compare(task=task, baseline=baseline, evolved=evolved, pair_id=pair_id)
        if ledger:
            ledger.terminal(
                "final_evaluator",
                {"mapping_seal_sha256": final.mapping_seal_sha256, "winner": final.winner, "pair_scores_sha256": canonical_sha256(final.model_dump(mode="json"))},
            )
        event_order.append("final_evaluator")
        if self.real_mode:
            assert_real_metric_observations(baseline.observations + evolved.observations)
        item_root = self.run_root / pair_id
        item_root.mkdir(parents=True, exist_ok=True)
        baseline_path = item_root / "baseline.trajectory.json"
        evolved_path = item_root / "evolved.trajectory.json"
        feedback_path = item_root / "feedback.json"
        artifact_path = item_root / "artifact.receipt.json"
        baseline_path.write_text(baseline.model_dump_json(indent=2), encoding="utf-8")
        evolved_path.write_text(evolved.model_dump_json(indent=2), encoding="utf-8")
        feedback_path.write_text(
            json.dumps(feedback_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        artifact_path.write_text(artifact.model_dump_json(indent=2), encoding="utf-8")
        event_order.append("sealed")
        active_before_reset = self._active_artifact
        self._active_artifact = None
        reset_payload = {
            "task_id": task.task_id,
            "discarded_artifact_id": active_before_reset[1] if active_before_reset else None,
            "active_artifact_ids_after": [],
            "prior_artifact_ids_exported": [],
            "memory_after": [],
            "skill_bundle_after": [],
            "agent_system_after": [],
            "s0_hash_for_next_item": self.s0_hash,
        }
        reset_path = item_root / "reset.receipt.json"
        reset_path.write_text(json.dumps(reset_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        event_order.append("reset")
        result = PairResult(
            task_id=task.task_id,
            task_category=task.broad_category,
            s0_hash=self.s0_hash,
            baseline=baseline,
            runtime_feedback=runtime,
            evolution_feedback=evaluator_feedback,
            feedback_hash=feedback_hash(feedback_payload),
            artifact=artifact,
            evolved=evolved,
            final_evaluation=final,
            event_order=event_order,
            reset_receipt_sha256=file_sha256(reset_path),
        )
        (item_root / "pair.result.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
        self._write_human_packet(
            task=task,
            pair_id=pair_id,
            baseline=baseline,
            evolved=evolved,
            mapping_seal=final.mapping_seal_sha256,
        )
        return result

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
        mapping = {"A": "baseline" if baseline_is_a else "evolved", "B": "evolved" if baseline_is_a else "baseline"}
        mapping_seal = canonical_sha256({"pair_id": pair_id, "mapping": mapping})
        judged = self.final_evaluator.compare(task=task, answer_a=answer_a, answer_b=answer_b)
        if judged.winner not in {"A", "B", "tie"}:
            raise ValueError("final evaluator winner is invalid")
        baseline_scores = judged.scores_a if baseline_is_a else judged.scores_b
        evolved_scores = judged.scores_b if baseline_is_a else judged.scores_a
        if judged.winner == "tie":
            winner = "tie"
        else:
            winner = mapping[judged.winner]
        return PairwiseEvaluation(
            baseline_scores=baseline_scores,
            evolved_scores=evolved_scores,
            delta_chemical_correctness=(
                evolved_scores.chemical_correctness - baseline_scores.chemical_correctness
            ),
            delta_reasoning_quality=(
                evolved_scores.reasoning_quality - baseline_scores.reasoning_quality
            ),
            delta_task_completion=evolved_scores.task_completion - baseline_scores.task_completion,
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
    ) -> None:
        rng = random.Random(f"{self.random_seed}:{pair_id}")
        baseline_is_a = bool(rng.getrandbits(1))
        packet = {
            "schema_version": "chemcrow_human_review_packet_v1",
            "pair_id": pair_id,
            "task_id": task.task_id,
            "task_category": task.broad_category,
            "task_prompt": task.prompt,
            "A": baseline.answer if baseline_is_a else evolved.answer,
            "B": evolved.answer if baseline_is_a else baseline.answer,
            "rubric": ["chemical correctness", "reasoning quality", "task completion"],
            "mapping_seal_sha256": mapping_seal,
            "historical_answers_included": False,
        }
        review_dir = self.run_root / "human_review"
        review_dir.mkdir(parents=True, exist_ok=True)
        (review_dir / f"{pair_id}.blinded.json").write_text(
            json.dumps(packet, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
