from __future__ import annotations

from collections import Counter
from typing import Any

from .three_artifact_models import ThreeArtifactPairResult, three_artifact_protocol_label


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
