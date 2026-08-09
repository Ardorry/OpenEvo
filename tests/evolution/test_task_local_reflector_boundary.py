from __future__ import annotations

from types import SimpleNamespace

from openevo.evolution.methods import (
    _guard_generic_reflector_output,
    _redact_generic_reflector_prompt,
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
