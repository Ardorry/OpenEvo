from __future__ import annotations

import pytest

from openevo_chembench.temperature_full_evolve_v1.statistics import paired_binary_metrics_v1


def test_paired_statistics_match_transition_counts_and_exact_mcnemar() -> None:
    reference = (True,) * 80 + (True,) * 10 + (False,) * 5 + (False,) * 5
    candidate = (True,) * 80 + (False,) * 10 + (True,) * 5 + (False,) * 5
    metrics = paired_binary_metrics_v1(
        reference,
        candidate,
        bootstrap_seed=7,
        bootstrap_replicates=10_000,
    )
    assert metrics.n == 100
    assert metrics.reference_correct == 90
    assert metrics.candidate_correct == 85
    assert metrics.both_correct == 80
    assert metrics.reference_only_correct == 10
    assert metrics.candidate_only_correct == 5
    assert metrics.both_wrong == 5
    assert metrics.delta_percentage_points == -5.0
    assert metrics.negative_flip_rate == 0.1
    assert metrics.mcnemar_exact_p == pytest.approx(0.3017578125)
    assert metrics.reference_wilson_95[0] < 0.9 < metrics.reference_wilson_95[1]
    assert metrics.paired_bootstrap_delta_95_percentage_points[0] <= -5.0
    assert metrics.paired_bootstrap_delta_95_percentage_points[1] >= -5.0


def test_no_discordance_has_unit_mcnemar_and_zero_delta_ci() -> None:
    values = (True, False) * 50
    metrics = paired_binary_metrics_v1(
        values,
        values,
        bootstrap_replicates=10_000,
    )
    assert metrics.mcnemar_exact_p == 1.0
    assert metrics.delta_percentage_points == 0.0
    assert metrics.paired_bootstrap_delta_95_percentage_points == (0.0, 0.0)


def test_invalid_pair_or_too_few_bootstrap_replicates_is_rejected() -> None:
    with pytest.raises(TypeError):
        paired_binary_metrics_v1((True,), ())
    with pytest.raises(ValueError, match="bootstrap replicate count"):
        paired_binary_metrics_v1((True,), (False,), bootstrap_replicates=9999)
