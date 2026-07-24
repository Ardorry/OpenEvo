from __future__ import annotations

import math

from openevo_chembench.paired_statistics_v2 import (
    PairedItemResult,
    _exact_mcnemar_p_value,
    compare_paired_results,
)


def _record(
    index: int,
    baseline: str,
    evolved: str,
) -> PairedItemResult:
    return PairedItemResult(
        uid=f"{index:064x}",
        category="Name_Conversion",
        target="A",
        baseline_prediction=baseline,
        evolved_prediction=evolved,
        baseline_official_parsed=True,
        evolved_official_parsed=True,
        baseline_strict_parsed=True,
        evolved_strict_parsed=True,
    )


def test_paired_statistics_counts_directional_changes() -> None:
    report = compare_paired_results(
        [
            _record(1, "B", "A"),
            _record(2, "A", "B"),
            _record(3, "A", "A"),
            _record(4, "B", "B"),
        ],
        planned_pairs=4,
    )
    assert report["wrong_to_correct"] == 1
    assert report["correct_to_wrong"] == 1
    assert report["both_correct"] == 1
    assert report["both_wrong"] == 1
    assert report["mcnemar_exact_p_value"] == 1.0
    assert report["decision"] == "NO_GO"


def test_paired_bootstrap_is_deterministic() -> None:
    records = [_record(index, "B", "A") for index in range(500)]
    first = compare_paired_results(records, planned_pairs=500)
    second = compare_paired_results(records, planned_pairs=500)
    assert first == second
    assert first["decision"] == "GO"
    assert first["paired_bootstrap_95_ci"] == {
        "lower": 1.0,
        "upper": 1.0,
        "samples": 10_000,
        "seed": "openevo-chembench4k-frozen-generalization-v2-bootstrap10000",
    }


def test_security_violation_forces_no_go() -> None:
    records = [_record(index, "B", "A") for index in range(500)]
    violated = records[0]
    records[0] = PairedItemResult(
        uid=violated.uid,
        category=violated.category,
        target=violated.target,
        baseline_prediction=violated.baseline_prediction,
        evolved_prediction=violated.evolved_prediction,
        baseline_official_parsed=True,
        evolved_official_parsed=True,
        baseline_strict_parsed=True,
        evolved_strict_parsed=True,
        evolved_security_violation=True,
    )
    report = compare_paired_results(records, planned_pairs=500)
    assert report["security_violations"] == 1
    assert report["decision"] == "NO_GO"


def test_completed_parse_failure_is_valid_and_scores_incorrect() -> None:
    record = PairedItemResult(
        uid=f"{1:064x}",
        category="Name_Conversion",
        target="A",
        baseline_prediction=None,
        evolved_prediction="A",
        baseline_official_parsed=False,
        evolved_official_parsed=True,
        baseline_strict_parsed=False,
        evolved_strict_parsed=True,
    )
    report = compare_paired_results([record], planned_pairs=1)
    assert report["valid_pairs"] == 1
    assert report["baseline_accuracy"] == 0.0
    assert report["evolved_accuracy"] == 1.0
    assert report["wrong_to_correct"] == 1


def test_non_choice_first_capital_is_a_parsed_wrong_prediction() -> None:
    record = PairedItemResult(
        uid=f"{2:064x}",
        category="Name_Conversion",
        target="A",
        baseline_prediction="T",
        evolved_prediction="A",
        baseline_official_parsed=True,
        evolved_official_parsed=True,
        baseline_strict_parsed=False,
        evolved_strict_parsed=True,
    )

    report = compare_paired_results([record], planned_pairs=1)

    assert report["valid_pairs"] == 1
    assert report["baseline_accuracy"] == 0.0
    assert report["evolved_accuracy"] == 1.0
    assert report["baseline_official_parse_rate"] == 1.0
    assert report["baseline_strict_parse_rate"] == 0.0
    assert report["wrong_to_correct"] == 1


def test_full_scale_mcnemar_does_not_overflow() -> None:
    value = _exact_mcnemar_p_value(4_009, 0)
    assert math.isfinite(value)
    assert value == 0.0
