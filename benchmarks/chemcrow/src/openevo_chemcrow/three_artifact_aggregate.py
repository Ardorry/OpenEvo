from __future__ import annotations

from collections import Counter
from typing import Any

from .three_artifact_models import (
    ThreeArtifactGenerationResult,
    ThreeArtifactPairResult,
    three_artifact_protocol_label,
)


def aggregate_three_artifact_results(
    results: list[ThreeArtifactPairResult],
) -> dict[str, Any]:
    if not results:
        return {
            "schema_version": "chemcrow_three_artifact_aggregate_v1",
            "task_count": 0,
            "status": "NO_RESULTS",
        }

    def mean(values: list[float]) -> float:
        return sum(values) / len(values)

    baseline = [item.final_evaluation.baseline_scores for item in results]
    evolved = [item.final_evaluation.evolved_scores for item in results]
    winners = Counter(item.final_evaluation.winner for item in results)
    observations = [
        observation
        for item in results
        for run in (item.baseline, item.evolved)
        for observation in run.observations
    ]
    errors = [observation for observation in observations if observation.error]
    bundle_protocols = {item.artifact_bundle.protocol for item in results}
    if len(bundle_protocols) != 1:
        raise ValueError("cannot aggregate mixed three-artifact protocol versions")
    artifact_protocol = three_artifact_protocol_label(bundle_protocols.pop())
    return {
        "schema_version": "chemcrow_three_artifact_aggregate_v1",
        "status": "PROVISIONAL_LLM_JUDGED_RESULT",
        "artifact_protocol": artifact_protocol,
        "task_count": len(results),
        "artifact_count": 3 * len(results),
        "mean_baseline": {
            "chemical_correctness": mean([x.chemical_correctness for x in baseline]),
            "reasoning_quality": mean([x.reasoning_quality for x in baseline]),
            "task_completion": mean([x.task_completion for x in baseline]),
        },
        "mean_evolved": {
            "chemical_correctness": mean([x.chemical_correctness for x in evolved]),
            "reasoning_quality": mean([x.reasoning_quality for x in evolved]),
            "task_completion": mean([x.task_completion for x in evolved]),
        },
        "mean_delta": {
            "chemical_correctness": mean(
                [item.final_evaluation.delta_chemical_correctness for item in results]
            ),
            "reasoning_quality": mean(
                [item.final_evaluation.delta_reasoning_quality for item in results]
            ),
            "task_completion": mean(
                [item.final_evaluation.delta_task_completion for item in results]
            ),
        },
        "pairwise": {
            "wins": winners["evolved"],
            "losses": winners["baseline"],
            "ties": winners["tie"],
        },
        "tool_observations": len(observations),
        "tool_success_rate": (
            (len(observations) - len(errors)) / len(observations) if observations else None
        ),
        "tool_error_rate": len(errors) / len(observations) if observations else None,
        "wall_time_seconds": sum(
            item.baseline.wall_time_seconds + item.evolved.wall_time_seconds for item in results
        ),
        "token_metadata_available": any(
            run.token_metadata for item in results for run in (item.baseline, item.evolved)
        ),
        "cost_metadata_available": any(
            run.cost_metadata for item in results for run in (item.baseline, item.evolved)
        ),
    }


def aggregate_three_artifact_generation_results(
    results: list[ThreeArtifactGenerationResult],
) -> dict[str, Any]:
    """Aggregate a sealed G1-plus-artifacts study without implying a G2 result."""

    if not results:
        return {
            "schema_version": "chemcrow_three_artifact_generation_aggregate_v1",
            "task_count": 0,
            "status": "NO_RESULTS",
        }

    def mean(values: list[float]) -> float:
        return sum(values) / len(values)

    protocols = {item.artifact_bundle.protocol for item in results}
    profiles = {item.artifact_bundle.prompt_profile for item in results}
    if len(protocols) != 1 or len(profiles) != 1:
        raise ValueError("cannot aggregate mixed artifact-generation protocols")
    scores = [item.baseline_internal_evaluation.scores for item in results]
    artifacts = [artifact for item in results for artifact in item.artifact_bundle.artifacts]
    observations = [observation for item in results for observation in item.baseline.observations]
    errors = [observation for observation in observations if observation.error]
    return {
        "schema_version": "chemcrow_three_artifact_generation_aggregate_v1",
        "status": "G1_AND_ARTIFACT_GENERATION_COMPLETE",
        "artifact_protocol": three_artifact_protocol_label(protocols.pop()),
        "prompt_profile": profiles.pop(),
        "task_count": len(results),
        "g1_candidate_call_count": len(results),
        "g1_internal_evaluator_call_count": len(results),
        "reflector_job_count": len(artifacts),
        "reflector_model_invocation_count": sum(
            len(artifact.reflector_attempt_run_ids) or 1 for artifact in artifacts
        ),
        "artifact_count": len(artifacts),
        "unique_artifact_count": len({artifact.artifact_id for artifact in artifacts}),
        "unique_reflector_job_count": len(
            {artifact.reflector_job_id for artifact in artifacts}
        ),
        "g2_candidate_call_count": 0,
        "g2_internal_evaluator_call_count": 0,
        "final_evaluator_call_count": 0,
        "mean_g1_internal": {
            "chemical_correctness": mean([score.chemical_correctness for score in scores]),
            "reasoning_quality": mean([score.reasoning_quality for score in scores]),
            "task_completion": mean([score.task_completion for score in scores]),
            "total": mean(
                [
                    score.chemical_correctness
                    + score.reasoning_quality
                    + score.task_completion
                    for score in scores
                ]
            ),
        },
        "tool_observations": len(observations),
        "tool_success_rate": (
            (len(observations) - len(errors)) / len(observations) if observations else None
        ),
        "tool_error_rate": len(errors) / len(observations) if observations else None,
        "wall_time_seconds": sum(item.baseline.wall_time_seconds for item in results),
        "token_metadata_available": any(item.baseline.token_metadata for item in results),
        "cost_metadata_available": any(item.baseline.cost_metadata for item in results),
    }
