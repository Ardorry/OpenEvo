from __future__ import annotations

from types import SimpleNamespace

from openevo.evolution.methods import (
    _evolution_feedback_snippet_chars,
    _guard_generic_reflector_output,
    _redact_generic_reflector_prompt,
    _reflection_record,
)


def test_task_local_reflector_boundary_removes_evaluator_labels_from_prompt_and_output() -> None:
    text = (
        "The raw GT, checklist keyword, Judge reasoning, raw Judge response, rubric mode, "
        "target_study, target-image information, and _score.json must never be retained."
    )
    job = SimpleNamespace(
        config={
            "training_feedback_required": True,
            "task_local_preservation": {
                "schema_version": "openevo.task_local_preservation.v1",
                "scope": "next_session_only",
            },
            "agent_system_audit": {},
        }
    )

    prompt = _redact_generic_reflector_prompt(text, job=job, manifests=[])
    artifact, audit = _guard_generic_reflector_output(text, job=job, manifests=[])
    redacted = f"{prompt}\n{artifact}".casefold()

    for forbidden in (
        "raw gt",
        "checklist",
        "judge reasoning",
        "raw judge response",
        "rubric mode",
        "target_study",
        "target-image",
        "_score.json",
    ):
        assert forbidden not in redacted
    assert "restricted evaluation" in redacted
    assert audit["remaining_finding_count"] == 0


def test_task_local_reflector_keeps_bounded_multi_achievement_feedback() -> None:
    achievement_lines = [
        f"achievement_{index:02d} REQUIRED method public evidence role "
        + ("x" * 120)
        for index in range(1, 13)
    ]
    record = {
        "task_id": "task",
        "session_id": "session",
        "status": "COMPLETED",
        "evolution_feedback": {
            "balanced_context": achievement_lines
            + ["FINAL_SANITIZED_DIAGNOSIS_AND_TRACE"],
        },
    }
    task_local_job = SimpleNamespace(
        config={
            "training_feedback_required": True,
            "task_local_preservation": {
                "schema_version": "openevo.task_local_preservation.v1",
                "scope": "next_session_only",
            },
        }
    )
    ordinary_job = SimpleNamespace(config={})

    task_local = _reflection_record(
        record,
        evolution_feedback_limit=_evolution_feedback_snippet_chars(task_local_job),
    )
    ordinary = _reflection_record(
        record,
        evolution_feedback_limit=_evolution_feedback_snippet_chars(ordinary_job),
    )

    assert task_local is not None
    assert ordinary is not None
    assert "achievement_12" in task_local["evolution_feedback"]
    assert "FINAL_SANITIZED_DIAGNOSIS_AND_TRACE" in task_local["evolution_feedback"]
    assert "FINAL_SANITIZED_DIAGNOSIS_AND_TRACE" not in ordinary["evolution_feedback"]
    assert len(ordinary["evolution_feedback"]) == 2_000
