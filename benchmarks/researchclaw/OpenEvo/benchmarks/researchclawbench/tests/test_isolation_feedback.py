from __future__ import annotations

from pathlib import Path

import pytest
from openevo_researchclawbench.feedback_filter import (
    COST_UNAVAILABLE,
    FeedbackPolicyError,
    validate_feedback,
)
from openevo_researchclawbench.hashing import UnsafePathError
from openevo_researchclawbench.isolation import (
    reject_forbidden_prepare_sources,
    validate_candidate_view,
)


def _feedback() -> dict:
    return {
        "task_id": "Life_005",
        "attempt_id": "Life_005_a0",
        "completed": True,
        "exit_code": 0,
        "artifact_valid": True,
        "total_score": 31.5,
        "generic_failure_tags": [],
        "runtime_bucket_seconds": 120,
        "cost_total_usd": 0.0,
        "artifact_root_sha256": "a" * 64,
    }


def test_feedback_accepts_only_scalar_total() -> None:
    assert validate_feedback(_feedback(), allowed_tasks={"Life_005"})["total_score"] == 31.5


def test_feedback_cost_can_be_explicitly_unavailable_but_not_faked() -> None:
    payload = _feedback()
    payload["cost_total_usd"] = COST_UNAVAILABLE
    assert (
        validate_feedback(payload, allowed_tasks={"Life_005"})["cost_total_usd"]
        == COST_UNAVAILABLE
    )
    payload["cost_total_usd"] = None
    with pytest.raises(FeedbackPolicyError, match="invalid cost"):
        validate_feedback(payload, allowed_tasks={"Life_005"})


@pytest.mark.parametrize(
    "field", ["items", "per_item_score", "judge_reasoning", "rubric_mode", "checklist"]
)
def test_feedback_rejects_private_fields(field: str) -> None:
    payload = _feedback()
    payload[field] = "private"
    with pytest.raises(FeedbackPolicyError):
        validate_feedback(payload, allowed_tasks={"Life_005"})


def test_feedback_rejects_validation_task() -> None:
    payload = _feedback()
    payload["task_id"] = "Earth_005"
    with pytest.raises(FeedbackPolicyError):
        validate_feedback(payload, allowed_tasks={"Life_005", "Neuroscience_004", "Earth_004"})


def _candidate_view(root: Path) -> None:
    root.mkdir()
    (root / "INSTRUCTIONS.md").write_text("task", encoding="utf-8")
    for name in ("data", "related_work", "code", "outputs", "artifacts", "skills"):
        (root / name).mkdir()
    (root / "report" / "images").mkdir(parents=True)


def test_candidate_view_is_closed_and_core_owned(tmp_path: Path) -> None:
    view = tmp_path / "view"
    _candidate_view(view)
    receipt = validate_candidate_view(view)
    assert receipt["managed_by"] == "OpenEvo Core managed runtime"
    assert receipt["target_study_visible"] is False


def test_target_study_prepare_source_is_rejected(tmp_path: Path) -> None:
    hidden = tmp_path / "target_study"
    hidden.mkdir()
    with pytest.raises(UnsafePathError):
        reject_forbidden_prepare_sources([hidden])


def test_candidate_view_rejects_extra_secret_entry(tmp_path: Path) -> None:
    view = tmp_path / "view"
    _candidate_view(view)
    (view / "secrets").mkdir()
    with pytest.raises(UnsafePathError):
        validate_candidate_view(view)
