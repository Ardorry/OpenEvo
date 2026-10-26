from __future__ import annotations

import json

from openevo_chemcrow.tasks import (
    extract_safety_demonstrations,
    extract_scored_tasks,
    write_jsonl,
)


def test_extracts_only_static_prompt_literals(runs_root, tmp_path):
    items, audit = extract_scored_tasks(runs_root)
    assert len(items) == 14
    assert audit["status"] == "PASS"
    assert audit["historical_notebook_outputs_included"] == 0
    assert all("cell_outputs_not_read" in item.provenance["extraction_method"] for item in items)
    assert sum(row["historical_output_objects_excluded"] for row in audit["items"]) > 0
    output = tmp_path / "tasks.jsonl"
    write_jsonl(items, output)
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    forbidden = {
        "answer",
        "trajectory",
        "reference_answer",
        "evaluator_feedback",
        "expert_grade",
        "result_tools",
    }
    assert all(not forbidden.intersection(row) for row in rows)
    safety_sensitive = [item for item in items if item.safety_metadata["safety_case"]]
    assert [item.task_id for item in safety_sensitive] == ["chemcrow-12"]
    assert safety_sensitive[0].safety_metadata["scored_task"] is True
    assert safety_sensitive[0].safety_metadata["safety_demonstration"] is False


def test_non_scored_and_safety_notebooks_are_separate(runs_root):
    _, audit = extract_scored_tasks(runs_root)
    excluded = audit["excluded_non_scored_notebooks"]
    assert "paper_figs/nitroglycerin_safety.ipynb" in excluded
    assert any(path.startswith("robotic_platform/") for path in excluded)
    safety = extract_safety_demonstrations(runs_root)
    assert [item.task_id for item in safety] == ["chemcrow-safety-nitroglycerin"]
    assert safety[0].safety_metadata["safety_case"] is True
    assert safety[0].safety_metadata["scored_task"] is False
