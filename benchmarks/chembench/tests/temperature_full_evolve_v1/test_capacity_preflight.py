from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from openevo_chembench.temperature_full_evolve_v1.capacity_preflight import (
    FINDING_CODE,
    build_capacity_preflight,
    select_equal_arm_size,
    uid_set_sha256,
)

REPOSITORY = Path(__file__).resolve().parents[4]


def _uid(index: int) -> str:
    return hashlib.sha256(f"synthetic-{index}".encode()).hexdigest()


@pytest.mark.parametrize(
    ("eligible_count", "expected"),
    (
        (400, 200),
        (399, 175),
        (350, 175),
        (349, 150),
        (300, 150),
        (299, 125),
        (250, 125),
        (249, 100),
        (200, 100),
        (199, None),
        (81, None),
    ),
)
def test_equal_arm_size_selection_is_closed_and_deterministic(
    eligible_count: int,
    expected: int | None,
) -> None:
    assert select_equal_arm_size(eligible_count) == expected


def test_uid_set_digest_is_order_independent_and_newline_bound() -> None:
    values = [_uid(2), _uid(1), _uid(2)]
    expected = hashlib.sha256(("\n".join(sorted(set(values))) + "\n").encode()).hexdigest()

    assert uid_set_sha256(values) == expected
    assert uid_set_sha256(reversed(values)) == expected


def test_live_temperature_capacity_preflight_fails_before_split_or_calls() -> None:
    receipt = build_capacity_preflight(REPOSITORY.resolve())

    assert receipt["status"] == "BLOCKED"
    assert receipt["finding_code"] == FINDING_CODE
    assert receipt["dataset"]["temperature_test_count"] == 202
    assert receipt["dataset"]["temperature_dev_count"] == 5
    assert receipt["historical_exposure"]["prior_snapshot_actual_count"] == 26
    assert receipt["historical_exposure"]["actual_exposed_union_count"] == 121
    assert (
        receipt["capacity_gate"][
            "maximum_provable_never_exposed_count_before_near_duplicate_grouping"
        ]
        == 81
    )
    assert receipt["capacity_gate"]["capacity_shortfall_before_near_duplicate_grouping"] == 119
    assert receipt["capacity_gate"]["selected_equal_arm_size"] is None
    assert receipt["capacity_gate"]["split_created"] is False
    assert receipt["capacity_gate"]["split_sha256"] is None
    assert receipt["side_effects"] == {
        "formal_run_created": False,
        "tmux_session_created": False,
        "candidate_calls": 0,
        "reflector_calls": 0,
        "core_jobs": 0,
        "baseline_calls": 0,
        "total_model_calls": 0,
    }


def test_live_receipt_is_aggregate_only() -> None:
    receipt = build_capacity_preflight(REPOSITORY.resolve())
    forbidden_keys = {
        "uid",
        "uids",
        "question",
        "options",
        "answer",
        "target",
        "prediction",
        "completion",
        "transcript",
        "api_key",
        "secret",
    }

    def visit(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                assert str(key).casefold() not in forbidden_keys
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(receipt)
