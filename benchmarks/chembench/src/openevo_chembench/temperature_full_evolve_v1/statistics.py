"""Deterministic paired binary statistics for Temperature full-evolve v1."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PairedBinaryMetricsV1:
    n: int
    reference_correct: int
    candidate_correct: int
    both_correct: int
    reference_only_correct: int
    candidate_only_correct: int
    both_wrong: int
    reference_accuracy: float
    candidate_accuracy: float
    reference_wilson_95: tuple[float, float]
    candidate_wilson_95: tuple[float, float]
    delta_percentage_points: float
    paired_bootstrap_delta_95_percentage_points: tuple[float, float]
    negative_flip_rate: float
    mcnemar_exact_p: float
    bootstrap_replicates: int
    bootstrap_seed: int

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "TemperaturePairedBinaryMetricsV1",
            "n": self.n,
            "reference_correct": self.reference_correct,
            "candidate_correct": self.candidate_correct,
            "reference_accuracy": self.reference_accuracy,
            "candidate_accuracy": self.candidate_accuracy,
            "reference_wilson_95": list(self.reference_wilson_95),
            "candidate_wilson_95": list(self.candidate_wilson_95),
            "delta_percentage_points": self.delta_percentage_points,
            "paired_bootstrap_delta_95_percentage_points": list(
                self.paired_bootstrap_delta_95_percentage_points
            ),
            "both_correct": self.both_correct,
            "reference_only_correct": self.reference_only_correct,
            "candidate_only_correct": self.candidate_only_correct,
            "both_wrong": self.both_wrong,
            "negative_flip_rate": self.negative_flip_rate,
            "mcnemar_exact_p": self.mcnemar_exact_p,
            "bootstrap_replicates": self.bootstrap_replicates,
            "bootstrap_seed": self.bootstrap_seed,
        }


def paired_binary_metrics_v1(
    reference: tuple[bool, ...],
    candidate: tuple[bool, ...],
    *,
    bootstrap_seed: int = 20260802,
    bootstrap_replicates: int = 100_000,
) -> PairedBinaryMetricsV1:
    """Compute Wilson intervals, exact McNemar, and paired bootstrap CI."""

    if (
        type(reference) is not tuple
        or type(candidate) is not tuple
        or not reference
        or len(reference) != len(candidate)
        or any(type(value) is not bool for value in (*reference, *candidate))
    ):
        raise TypeError("paired inputs must be equal non-empty exact bool tuples")
    if type(bootstrap_seed) is not int or type(bootstrap_replicates) is not int:
        raise TypeError("bootstrap settings must be integers")
    if bootstrap_replicates < 10_000 or bootstrap_replicates > 1_000_000:
        raise ValueError("bootstrap replicate count is outside the frozen range")
    n = len(reference)
    both_correct = sum(left and right for left, right in zip(reference, candidate, strict=True))
    reference_only = sum(left and not right for left, right in zip(reference, candidate, strict=True))
    candidate_only = sum(not left and right for left, right in zip(reference, candidate, strict=True))
    both_wrong = n - both_correct - reference_only - candidate_only
    reference_correct = both_correct + reference_only
    candidate_correct = both_correct + candidate_only
    differences = tuple(int(right) - int(left) for left, right in zip(reference, candidate, strict=True))
    bootstrap = _paired_bootstrap_percentile(
        differences,
        seed=bootstrap_seed,
        replicates=bootstrap_replicates,
    )
    return PairedBinaryMetricsV1(
        n=n,
        reference_correct=reference_correct,
        candidate_correct=candidate_correct,
        both_correct=both_correct,
        reference_only_correct=reference_only,
        candidate_only_correct=candidate_only,
        both_wrong=both_wrong,
        reference_accuracy=reference_correct / n,
        candidate_accuracy=candidate_correct / n,
        reference_wilson_95=_wilson_95(reference_correct, n),
        candidate_wilson_95=_wilson_95(candidate_correct, n),
        delta_percentage_points=100.0 * (candidate_correct - reference_correct) / n,
        paired_bootstrap_delta_95_percentage_points=bootstrap,
        negative_flip_rate=reference_only / n,
        mcnemar_exact_p=_mcnemar_exact_two_sided(reference_only, candidate_only),
        bootstrap_replicates=bootstrap_replicates,
        bootstrap_seed=bootstrap_seed,
    )


def _wilson_95(successes: int, n: int) -> tuple[float, float]:
    if not 0 <= successes <= n or n < 1:
        raise ValueError("Wilson inputs are invalid")
    z = 1.959963984540054
    proportion = successes / n
    denominator = 1.0 + z * z / n
    center = (proportion + z * z / (2.0 * n)) / denominator
    half = (
        z
        * math.sqrt(proportion * (1.0 - proportion) / n + z * z / (4.0 * n * n))
        / denominator
    )
    return (max(0.0, center - half), min(1.0, center + half))


def _mcnemar_exact_two_sided(reference_only: int, candidate_only: int) -> float:
    if reference_only < 0 or candidate_only < 0:
        raise ValueError("McNemar discordant counts must be non-negative")
    discordant = reference_only + candidate_only
    if discordant == 0:
        return 1.0
    smaller = min(reference_only, candidate_only)
    cumulative = sum(math.comb(discordant, value) for value in range(smaller + 1))
    return min(1.0, 2.0 * cumulative / (2**discordant))


def _paired_bootstrap_percentile(
    differences: tuple[int, ...],
    *,
    seed: int,
    replicates: int,
) -> tuple[float, float]:
    if not differences or any(value not in {-1, 0, 1} for value in differences):
        raise ValueError("paired bootstrap differences are invalid")
    generator = random.Random(seed)
    n = len(differences)
    samples = [
        100.0 * sum(generator.choice(differences) for _ in range(n)) / n
        for _ in range(replicates)
    ]
    samples.sort()
    lower = samples[math.floor(0.025 * (replicates - 1))]
    upper = samples[math.ceil(0.975 * (replicates - 1))]
    return (lower, upper)


__all__ = ["PairedBinaryMetricsV1", "paired_binary_metrics_v1"]
