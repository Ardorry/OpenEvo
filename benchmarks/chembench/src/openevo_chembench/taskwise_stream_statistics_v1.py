"""Stream-clustered statistics for the ten-chain taskwise online pilot."""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass
from typing import Any

from openevo_chembench.chembench4k_models import CHEMBENCH4K_CATEGORIES
from openevo_chembench.taskwise_reporting_v1 import (
    PROTOCOL_ID,
    PROTOCOL_LABELS,
    PrivateTaskwiseRoundResultV1,
    build_taskwise_online_report_v1,
)
from openevo_chembench.taskwise_sampling_v1 import (
    PILOT500_SIZE,
    PILOT500_STREAM_COUNT,
    PILOT500_STREAM_DESIGN,
    PILOT500_STREAM_SCOPES,
    PILOT500_TASKS_PER_STREAM,
)


STREAM_BOOTSTRAP_SAMPLES = 10_000
STREAM_BOOTSTRAP_SEED = "openevo-chembench4k-taskwise-online-v1-ten-stream-bootstrap10000"


@dataclass(frozen=True, slots=True, repr=False)
class PrivateTaskwisePairedStreamV1:
    """One target-bearing, independently initialized control/online stream pair."""

    stream_id: str
    control: tuple[PrivateTaskwiseRoundResultV1, ...]
    online: tuple[PrivateTaskwiseRoundResultV1, ...]
    control_security_violations: int = 0
    online_security_violations: int = 0
    online_artifact_validation_failures: int = 0
    context_binding_violations: int = 0
    completed: bool = True
    approved_memory_utf8_bytes: tuple[int, ...] = ()

    def __repr__(self) -> str:
        return "PrivateTaskwisePairedStreamV1(<redacted>)"

    __str__ = __repr__

    def __post_init__(self) -> None:
        if self.stream_id not in PILOT500_STREAM_SCOPES:
            raise ValueError("stream_id must be one of the ten frozen pilot streams")
        for value, field_name in (
            (self.control_security_violations, "control_security_violations"),
            (self.online_security_violations, "online_security_violations"),
            (
                self.online_artifact_validation_failures,
                "online_artifact_validation_failures",
            ),
            (self.context_binding_violations, "context_binding_violations"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")
        if type(self.completed) is not bool:
            raise TypeError("completed must be boolean")
        if type(self.approved_memory_utf8_bytes) is not tuple or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in self.approved_memory_utf8_bytes
        ):
            raise ValueError("approved memory byte metrics must be non-negative integers")


def build_taskwise_pilot500_stream_report_v1(
    streams: tuple[PrivateTaskwisePairedStreamV1, ...],
) -> dict[str, Any]:
    """Aggregate ten independent memory chains using stream-level inference."""

    if (
        type(streams) is not tuple
        or len(streams) != PILOT500_STREAM_COUNT
        or tuple(stream.stream_id for stream in streams) != PILOT500_STREAM_SCOPES
    ):
        raise ValueError("pilot report requires all ten frozen streams in order")

    per_stream: list[dict[str, Any]] = []
    all_uids: set[str] = set()
    stream_deltas: list[float] = []
    round_2_pairs: list[tuple[PrivateTaskwiseRoundResultV1, PrivateTaskwiseRoundResultV1]] = []
    position_rows: dict[
        int,
        list[tuple[PrivateTaskwiseRoundResultV1, PrivateTaskwiseRoundResultV1]],
    ] = {index: [] for index in range(PILOT500_TASKS_PER_STREAM)}
    total_security_violations = 0
    total_artifact_validation_failures = 0
    total_context_binding_violations = 0
    completed_streams = 0

    for stream in streams:
        report = build_taskwise_online_report_v1(
            control=stream.control,
            online=stream.online,
            control_security_violations=stream.control_security_violations,
            online_security_violations=stream.online_security_violations,
            online_artifact_validation_failures=(stream.online_artifact_validation_failures),
        )
        if report["task_count"] != PILOT500_TASKS_PER_STREAM:
            raise ValueError("each pilot stream must contain exactly 50 paired tasks")
        control_grouped = _group_rounds(stream.control, expected_arm="control")
        online_grouped = _group_rounds(stream.online, expected_arm="online")
        if set(control_grouped) != set(online_grouped):
            raise ValueError("control and online stream tasks must be exactly paired")
        stream_uids = set(control_grouped)
        if all_uids & stream_uids:
            raise ValueError("pilot streams must not share task UIDs")
        all_uids.update(stream_uids)
        for uid in sorted(
            stream_uids,
            key=lambda item: control_grouped[item][0].task_index,
        ):
            position = control_grouped[uid][0].task_index
            position_rows[position].append((control_grouped[uid][0], online_grouped[uid][0]))
            round_2_pairs.append((control_grouped[uid][2], online_grouped[uid][2]))

        delta = float(report["paired_final_round"]["absolute_delta"])
        stream_deltas.append(delta)
        total_security_violations += (
            stream.control_security_violations + stream.online_security_violations
        )
        total_artifact_validation_failures += stream.online_artifact_validation_failures
        total_context_binding_violations += stream.context_binding_violations
        completed_streams += int(stream.completed)
        if (
            stream.completed
            and len(stream.approved_memory_utf8_bytes) != 2 * PILOT500_TASKS_PER_STREAM
        ):
            raise ValueError("completed stream must expose 100 approved memory metrics")
        control_grouped = _group_rounds(stream.control, expected_arm="control")
        online_grouped = _group_rounds(stream.online, expected_arm="online")
        wrong_to_correct_0_to_1 = sum(
            (not rounds[0].correct) and rounds[1].correct for rounds in online_grouped.values()
        )
        wrong_to_correct_0_to_2 = sum(
            (not rounds[0].correct) and rounds[2].correct for rounds in online_grouped.values()
        )
        correct_to_wrong = sum(
            rounds[0].correct and (not rounds[2].correct) for rounds in online_grouped.values()
        )
        memory_values = stream.approved_memory_utf8_bytes
        per_stream.append(
            {
                "stream_id": stream.stream_id,
                "task_count": report["task_count"],
                "control_round_0_accuracy": report["control"]["round_0_accuracy"],
                "online_round_0_accuracy": report["online"]["round_0_accuracy"],
                "control_round_2_accuracy": report["control"]["round_2_accuracy"],
                "online_round_1_accuracy": report["online"]["round_1_accuracy"],
                "online_round_2_accuracy": report["online"]["round_2_accuracy"],
                "round_2_delta_vs_control": delta,
                "round_2_absolute_delta": delta,
                "round_0_transfer_delta_vs_control": (
                    report["online"]["round_0_accuracy"] - report["control"]["round_0_accuracy"]
                ),
                "wrong_to_correct_0_to_1": wrong_to_correct_0_to_1,
                "wrong_to_correct_0_to_2": wrong_to_correct_0_to_2,
                "correct_to_wrong": correct_to_wrong,
                "online_total_within_task_gain": report["online"]["total_within_task_gain"],
                "online_memory_chain_length": report["online"]["memory_chain_length"],
                "security_violations": (
                    stream.control_security_violations + stream.online_security_violations
                ),
                "artifact_validation_failures": (stream.online_artifact_validation_failures),
                "artifact_failure_count": (stream.online_artifact_validation_failures),
                "context_binding_violations": stream.context_binding_violations,
                "completed": stream.completed,
                "memory_growth": {
                    "first_approved_utf8_bytes": (None if not memory_values else memory_values[0]),
                    "last_approved_utf8_bytes": (None if not memory_values else memory_values[-1]),
                    "delta_utf8_bytes": (
                        None if not memory_values else memory_values[-1] - memory_values[0]
                    ),
                    "max_approved_utf8_bytes": (None if not memory_values else max(memory_values)),
                },
            }
        )

    if len(all_uids) != PILOT500_SIZE or len(round_2_pairs) != PILOT500_SIZE:
        raise ValueError("pilot stream report must cover exactly 500 unique tasks")
    if any(len(rows) != PILOT500_STREAM_COUNT for rows in position_rows.values()):
        raise ValueError("each within-stream position must have ten paired observations")

    control_round_2_correct = sum(left.correct for left, _right in round_2_pairs)
    online_round_2_correct = sum(right.correct for _left, right in round_2_pairs)
    absolute_delta = (online_round_2_correct - control_round_2_correct) / PILOT500_SIZE
    ci_lower, ci_upper = _stream_bootstrap_interval(stream_deltas)
    mean_delta = statistics.fmean(stream_deltas)
    median_delta = statistics.median(stream_deltas)
    std_delta = statistics.stdev(stream_deltas)
    positive_stream_count = sum(value > 0 for value in stream_deltas)
    wrong_to_correct = sum((not left.correct) and right.correct for left, right in round_2_pairs)
    correct_to_wrong = sum(left.correct and (not right.correct) for left, right in round_2_pairs)
    control_strict = (
        sum(
            row.strict_parsed
            for stream in streams
            for row in stream.control
            if row.round_index == 2
        )
        / PILOT500_SIZE
    )
    online_strict = (
        sum(
            row.strict_parsed
            for stream in streams
            for row in stream.online
            if row.round_index == 2
        )
        / PILOT500_SIZE
    )
    go = (
        completed_streams == PILOT500_STREAM_COUNT
        and total_security_violations == 0
        and total_artifact_validation_failures == 0
        and total_context_binding_violations == 0
        and mean_delta >= 0.02
        and ci_lower > 0.0
        and positive_stream_count >= 7
    )
    return {
        "schema_version": "taskwise_online_pilot500_ten_stream_report_v1",
        "protocol_id": PROTOCOL_ID,
        "protocol_labels": list(PROTOCOL_LABELS),
        "standard_chembench4k_score_claimed": False,
        "selection_policy": "fixed_round_2_no_best_of_no_early_stop",
        "stream_design": PILOT500_STREAM_DESIGN,
        "inference_unit": "independent_task_stream",
        "stream_count": PILOT500_STREAM_COUNT,
        "tasks_per_stream": PILOT500_TASKS_PER_STREAM,
        "task_count": PILOT500_SIZE,
        "status": "COMPLETED",
        "decision": "GO" if go else "NO_GO",
        "completed_streams": completed_streams,
        "per_stream": per_stream,
        "paired_final_round": {
            "comparison": "online_round_2_vs_control_round_2",
            "control_round_2_accuracy": control_round_2_correct / PILOT500_SIZE,
            "online_round_2_accuracy": online_round_2_correct / PILOT500_SIZE,
            "absolute_delta": absolute_delta,
            "stream_delta_mean": mean_delta,
            "stream_delta_median": median_delta,
            "stream_delta_std": std_delta,
            "positive_stream_count": positive_stream_count,
            "wrong_to_correct": wrong_to_correct,
            "correct_to_wrong": correct_to_wrong,
            "both_correct": sum(left.correct and right.correct for left, right in round_2_pairs),
            "both_wrong": sum(
                (not left.correct) and (not right.correct) for left, right in round_2_pairs
            ),
            "stream_exact_sign_flip_p_value": _exact_stream_sign_flip_p_value(stream_deltas),
            "stream_block_bootstrap_95_ci": {
                "lower": ci_lower,
                "upper": ci_upper,
                "samples": STREAM_BOOTSTRAP_SAMPLES,
                "seed": STREAM_BOOTSTRAP_SEED,
            },
        },
        "dependent_task_observations": {
            "classification": "DESCRIPTIVE_ONLY_DEPENDENT_TASK_OBSERVATIONS",
            "round_0_accuracy_by_within_stream_position": [
                {
                    "stream_position": position,
                    "control_accuracy": (
                        sum(left.correct for left, _right in rows) / PILOT500_STREAM_COUNT
                    ),
                    "online_accuracy": (
                        sum(right.correct for _left, right in rows) / PILOT500_STREAM_COUNT
                    ),
                }
                for position, rows in sorted(position_rows.items())
            ],
        },
        "per_category_round_2": _pilot_per_category_round_2(round_2_pairs),
        "control_round_2_strict_parse_rate": control_strict,
        "online_round_2_strict_parse_rate": online_strict,
        "infrastructure_failures": 0,
        "security_violations": total_security_violations,
        "artifact_validation_failures": total_artifact_validation_failures,
        "context_binding_violations": total_context_binding_violations,
        "go_gate": {
            "completed_streams_required": PILOT500_STREAM_COUNT,
            "stream_count_required": PILOT500_STREAM_COUNT,
            "task_count_required": PILOT500_SIZE,
            "security_violations_required": 0,
            "artifact_validation_failures_required": 0,
            "context_binding_violations_required": 0,
            "mean_stream_delta_minimum": 0.02,
            "stream_block_bootstrap_ci_lower_must_exceed": 0.0,
            "positive_stream_count_minimum": 7,
        },
    }


def _group_rounds(
    rows: tuple[PrivateTaskwiseRoundResultV1, ...],
    *,
    expected_arm: str,
) -> dict[str, dict[int, PrivateTaskwiseRoundResultV1]]:
    grouped: dict[str, dict[int, PrivateTaskwiseRoundResultV1]] = {}
    indices: dict[str, int] = {}
    for row in rows:
        if type(row) is not PrivateTaskwiseRoundResultV1 or row.arm != expected_arm:
            raise ValueError("stream report arm rows are invalid")
        indices[row.task_uid] = row.task_index
        rounds = grouped.setdefault(row.task_uid, {})
        if row.round_index in rounds:
            raise ValueError("stream report contains a duplicate round")
        rounds[row.round_index] = row
    if any(set(rounds) != {0, 1, 2} for rounds in grouped.values()):
        raise ValueError("each stream task must contain all three rounds")
    if sorted(indices.values()) != list(range(len(indices))):
        raise ValueError("stream task indices must be a complete zero-based range")
    return grouped


def _stream_bootstrap_interval(
    stream_deltas: list[float],
) -> tuple[float, float]:
    if len(stream_deltas) != PILOT500_STREAM_COUNT:
        raise ValueError("stream bootstrap requires exactly ten stream deltas")
    generator = random.Random(int.from_bytes(STREAM_BOOTSTRAP_SEED.encode("utf-8"), "big"))
    estimates = [
        sum(
            stream_deltas[generator.randrange(PILOT500_STREAM_COUNT)]
            for _index in range(PILOT500_STREAM_COUNT)
        )
        / PILOT500_STREAM_COUNT
        for _sample in range(STREAM_BOOTSTRAP_SAMPLES)
    ]
    estimates.sort()
    return (
        _percentile(estimates, 0.025),
        _percentile(estimates, 0.975),
    )


def _percentile(values: list[float], probability: float) -> float:
    rank = (len(values) - 1) * probability
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return values[lower]
    fraction = rank - lower
    return values[lower] * (1.0 - fraction) + values[upper] * fraction


def _exact_stream_sign_flip_p_value(stream_deltas: list[float]) -> float:
    if len(stream_deltas) != PILOT500_STREAM_COUNT:
        raise ValueError("sign-flip test requires exactly ten stream deltas")
    observed = abs(sum(stream_deltas) / PILOT500_STREAM_COUNT)
    extreme = 0
    assignments = 1 << PILOT500_STREAM_COUNT
    for mask in range(assignments):
        estimate = (
            sum(
                value if mask & (1 << index) else -value
                for index, value in enumerate(stream_deltas)
            )
            / PILOT500_STREAM_COUNT
        )
        if abs(estimate) + 1e-15 >= observed:
            extreme += 1
    return extreme / assignments


def _pilot_per_category_round_2(
    pairs: list[tuple[PrivateTaskwiseRoundResultV1, PrivateTaskwiseRoundResultV1]],
) -> dict[str, dict[str, int | float]]:
    buckets: dict[
        str,
        list[tuple[PrivateTaskwiseRoundResultV1, PrivateTaskwiseRoundResultV1]],
    ] = {category: [] for category in CHEMBENCH4K_CATEGORIES}
    for pair in pairs:
        if pair[0].category != pair[1].category:
            raise ValueError("paired task categories must match")
        buckets[pair[0].category].append(pair)
    result: dict[str, dict[str, int | float]] = {}
    for category, rows in buckets.items():
        if not rows:
            raise ValueError("pilot stream suite must cover all categories")
        control_accuracy = sum(left.correct for left, _right in rows) / len(rows)
        online_accuracy = sum(right.correct for _left, right in rows) / len(rows)
        result[category] = {
            "task_count": len(rows),
            "control_round_2_accuracy": control_accuracy,
            "online_round_2_accuracy": online_accuracy,
            "absolute_delta": online_accuracy - control_accuracy,
        }
    return result


__all__ = [
    "STREAM_BOOTSTRAP_SAMPLES",
    "STREAM_BOOTSTRAP_SEED",
    "PrivateTaskwisePairedStreamV1",
    "build_taskwise_pilot500_stream_report_v1",
]
