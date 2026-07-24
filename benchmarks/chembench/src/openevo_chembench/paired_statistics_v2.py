"""Deterministic paired statistics and preregistered GO gate for ChemBench4K v2."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Any


BOOTSTRAP_SEED = "openevo-chembench4k-frozen-generalization-v2-bootstrap10000"
BOOTSTRAP_SAMPLES = 10_000


@dataclass(frozen=True, slots=True, repr=False)
class PairedItemResult:
    """Private paired evaluator record; target and predictions are never reported."""

    uid: str
    category: str
    target: str
    baseline_prediction: str | None
    evolved_prediction: str | None
    baseline_official_parsed: bool
    evolved_official_parsed: bool
    baseline_strict_parsed: bool
    evolved_strict_parsed: bool
    baseline_infrastructure_failure: bool = False
    evolved_infrastructure_failure: bool = False
    baseline_security_violation: bool = False
    evolved_security_violation: bool = False

    def __post_init__(self) -> None:
        if not self.uid or not self.category:
            raise ValueError("paired record identity must be non-empty")
        if self.target not in {"A", "B", "C", "D"}:
            raise ValueError("private paired target must be A/B/C/D")
        for prediction in (self.baseline_prediction, self.evolved_prediction):
            if prediction not in {None, ""} and (
                type(prediction) is not str or len(prediction) != 1 or not prediction.isupper()
            ):
                raise ValueError(
                    "official prediction must be empty, one uppercase character, or None"
                )
        for field_name in (
            "baseline_official_parsed",
            "evolved_official_parsed",
            "baseline_strict_parsed",
            "evolved_strict_parsed",
            "baseline_infrastructure_failure",
            "evolved_infrastructure_failure",
            "baseline_security_violation",
            "evolved_security_violation",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise TypeError(f"{field_name} must be boolean")


def _exact_mcnemar_p_value(wrong_to_correct: int, correct_to_wrong: int) -> float:
    discordant = wrong_to_correct + correct_to_wrong
    if discordant == 0:
        return 1.0
    tail = min(wrong_to_correct, correct_to_wrong)
    # Summing exact integers and then converting the ratio to float overflows
    # for the 4,009-item full split.  A log-sum-exp binomial CDF is stable for
    # both the pilot and full protocol while retaining deterministic stdlib
    # behavior.
    log_probabilities = [
        (
            math.lgamma(discordant + 1)
            - math.lgamma(index + 1)
            - math.lgamma(discordant - index + 1)
            - discordant * math.log(2.0)
        )
        for index in range(tail + 1)
    ]
    maximum = max(log_probabilities)
    log_tail_probability = maximum + math.log(
        sum(math.exp(value - maximum) for value in log_probabilities)
    )
    return min(1.0, 2.0 * math.exp(log_tail_probability))


def _percentile(sorted_values: list[float], probability: float) -> float:
    if not sorted_values:
        raise ValueError("percentile requires values")
    rank = (len(sorted_values) - 1) * probability
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return sorted_values[lower]
    fraction = rank - lower
    return sorted_values[lower] * (1.0 - fraction) + sorted_values[upper] * fraction


def _bootstrap_delta_interval(
    correctness_pairs: list[tuple[int, int]],
    *,
    samples: int = BOOTSTRAP_SAMPLES,
    seed: str = BOOTSTRAP_SEED,
) -> tuple[float, float]:
    if not correctness_pairs:
        return (0.0, 0.0)
    if samples != BOOTSTRAP_SAMPLES or seed != BOOTSTRAP_SEED:
        raise ValueError("bootstrap parameters are frozen by protocol")
    seed_integer = int.from_bytes(seed.encode("utf-8"), "big")
    generator = random.Random(seed_integer)
    size = len(correctness_pairs)
    deltas: list[float] = []
    for _ in range(samples):
        total = 0
        for _index in range(size):
            baseline, evolved = correctness_pairs[generator.randrange(size)]
            total += evolved - baseline
        deltas.append(total / size)
    deltas.sort()
    return (_percentile(deltas, 0.025), _percentile(deltas, 0.975))


def compare_paired_results(
    records: list[PairedItemResult],
    *,
    planned_pairs: int,
) -> dict[str, Any]:
    """Compute the frozen paired report without exposing private labels."""

    if isinstance(planned_pairs, bool) or not isinstance(planned_pairs, int):
        raise TypeError("planned_pairs must be an integer")
    if planned_pairs < 1 or len(records) > planned_pairs:
        raise ValueError("planned_pairs must cover every supplied record")
    if len({record.uid for record in records}) != len(records):
        raise ValueError("paired result UIDs must be unique")

    valid = [
        record
        for record in records
        if not (
            record.baseline_infrastructure_failure
            or record.evolved_infrastructure_failure
            or record.baseline_security_violation
            or record.evolved_security_violation
        )
    ]
    correctness_pairs = [
        (
            int(record.baseline_prediction == record.target),
            int(record.evolved_prediction == record.target),
        )
        for record in valid
    ]
    baseline_correct = sum(pair[0] for pair in correctness_pairs)
    evolved_correct = sum(pair[1] for pair in correctness_pairs)
    valid_pairs = len(valid)
    baseline_accuracy = baseline_correct / valid_pairs if valid_pairs else 0.0
    evolved_accuracy = evolved_correct / valid_pairs if valid_pairs else 0.0
    delta = evolved_accuracy - baseline_accuracy
    baseline_error = 1.0 - baseline_accuracy
    relative_error_reduction = delta / baseline_error if baseline_error > 0 else None

    wrong_to_correct = sum(pair == (0, 1) for pair in correctness_pairs)
    correct_to_wrong = sum(pair == (1, 0) for pair in correctness_pairs)
    both_correct = sum(pair == (1, 1) for pair in correctness_pairs)
    both_wrong = sum(pair == (0, 0) for pair in correctness_pairs)
    ci_lower, ci_upper = _bootstrap_delta_interval(correctness_pairs)

    categories: dict[str, dict[str, int]] = {}
    for record, pair in zip(valid, correctness_pairs, strict=True):
        bucket = categories.setdefault(
            record.category,
            {"valid_pairs": 0, "baseline_correct": 0, "evolved_correct": 0},
        )
        bucket["valid_pairs"] += 1
        bucket["baseline_correct"] += pair[0]
        bucket["evolved_correct"] += pair[1]
    per_category: dict[str, dict[str, float | int]] = {}
    for category, bucket in sorted(categories.items()):
        count = bucket["valid_pairs"]
        baseline_value = bucket["baseline_correct"] / count
        evolved_value = bucket["evolved_correct"] / count
        per_category[category] = {
            "valid_pairs": count,
            "baseline_accuracy": baseline_value,
            "evolved_accuracy": evolved_value,
            "absolute_delta": evolved_value - baseline_value,
        }

    security_violations = sum(
        int(record.baseline_security_violation) + int(record.evolved_security_violation)
        for record in records
    )
    infrastructure_failures = sum(
        int(record.baseline_infrastructure_failure) + int(record.evolved_infrastructure_failure)
        for record in records
    )
    infrastructure_failure_rate = infrastructure_failures / (2 * planned_pairs)
    baseline_official_parse_rate = (
        sum(record.baseline_official_parsed for record in records) / planned_pairs
    )
    evolved_official_parse_rate = (
        sum(record.evolved_official_parsed for record in records) / planned_pairs
    )
    baseline_strict_parse_rate = (
        sum(record.baseline_strict_parsed for record in records) / planned_pairs
    )
    evolved_strict_parse_rate = (
        sum(record.evolved_strict_parsed for record in records) / planned_pairs
    )
    go = (
        valid_pairs >= 495
        and security_violations == 0
        and baseline_strict_parse_rate >= 0.995
        and evolved_strict_parse_rate >= 0.995
        and infrastructure_failure_rate <= 0.005
        and delta >= 0.02
        and ci_lower > 0
    )

    return {
        "schema_version": "chembench4k_paired_comparison_v2",
        "protocol_id": "chembench4k_frozen_generalization_v2",
        "decision": "GO" if go else "NO_GO",
        "planned_pairs": planned_pairs,
        "valid_pairs": valid_pairs,
        "baseline_accuracy": baseline_accuracy,
        "evolved_accuracy": evolved_accuracy,
        "absolute_delta": delta,
        "relative_error_reduction": relative_error_reduction,
        "baseline_official_parse_rate": baseline_official_parse_rate,
        "evolved_official_parse_rate": evolved_official_parse_rate,
        "baseline_strict_parse_rate": baseline_strict_parse_rate,
        "evolved_strict_parse_rate": evolved_strict_parse_rate,
        "wrong_to_correct": wrong_to_correct,
        "correct_to_wrong": correct_to_wrong,
        "both_correct": both_correct,
        "both_wrong": both_wrong,
        "mcnemar_exact_p_value": _exact_mcnemar_p_value(
            wrong_to_correct,
            correct_to_wrong,
        ),
        "paired_bootstrap_95_ci": {
            "lower": ci_lower,
            "upper": ci_upper,
            "samples": BOOTSTRAP_SAMPLES,
            "seed": BOOTSTRAP_SEED,
        },
        "per_category": per_category,
        "infrastructure_failures": infrastructure_failures,
        "infrastructure_failure_rate": infrastructure_failure_rate,
        "security_violations": security_violations,
        "go_gate": {
            "valid_pairs_minimum": 495,
            "strict_parse_rate_minimum": 0.995,
            "infrastructure_failure_rate_maximum": 0.005,
            "absolute_delta_minimum": 0.02,
            "paired_bootstrap_ci_lower_must_exceed": 0.0,
            "security_violations_required": 0,
        },
    }


__all__ = [
    "BOOTSTRAP_SAMPLES",
    "BOOTSTRAP_SEED",
    "PairedItemResult",
    "compare_paired_results",
]
