from __future__ import annotations

from collections import Counter
from typing import Any

from .models import PairResult


def aggregate_results(results: list[PairResult]) -> dict[str, Any]:
    if not results:
        return {"task_count": 0, "status": "NO_RESULTS"}

    def mean(values: list[float]) -> float:
        return sum(values) / len(values)

    baseline = [item.final_evaluation.baseline_scores for item in results]
    evolved = [item.final_evaluation.evolved_scores for item in results]
    winners = Counter(item.final_evaluation.winner for item in results)
    observations = [obs for item in results for run in (item.baseline, item.evolved) for obs in run.observations]
    tool_calls = [call for item in results for run in (item.baseline, item.evolved) for call in run.tool_calls]
    errors = [obs for obs in observations if obs.error]
    return {
        "schema_version": "chemcrow_aggregate_v1",
        "status": "PROVISIONAL_LLM_JUDGED_RESULT",
        "task_count": len(results),
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
            "chemical_correctness": mean([item.final_evaluation.delta_chemical_correctness for item in results]),
            "reasoning_quality": mean([item.final_evaluation.delta_reasoning_quality for item in results]),
            "task_completion": mean([item.final_evaluation.delta_task_completion for item in results]),
        },
        "pairwise": {"wins": winners["evolved"], "losses": winners["baseline"], "ties": winners["tie"]},
        "tool_calls": len(tool_calls),
        "tool_observations": len(observations),
        "tool_success_rate": (len(observations) - len(errors)) / len(observations) if observations else None,
        "tool_error_rate": len(errors) / len(observations) if observations else None,
        "wall_time_seconds": sum(item.baseline.wall_time_seconds + item.evolved.wall_time_seconds for item in results),
        "token_metadata_available": any(run.token_metadata for item in results for run in (item.baseline, item.evolved)),
        "cost_metadata_available": any(run.cost_metadata for item in results for run in (item.baseline, item.evolved)),
    }
