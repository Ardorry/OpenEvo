from __future__ import annotations

import hashlib
import json
from pathlib import Path

from openevo_researchclawbench.gt_supervision import (
    load_current_task_gt_supervision,
)


class _Config:
    def __init__(self, root: Path) -> None:
        self.researchclawbench_root = root


def test_current_task_gt_adapter_is_task_local_and_judge_free(tmp_path: Path) -> None:
    root = tmp_path / "ResearchClawBench"
    target = root / "tasks" / "Life_005" / "target_study"
    target.mkdir(parents=True)
    payload = [
        {
            "path": "report/report.md",
            "type": "contains",
            "content": "expected result",
            "keywords": ["expected"],
            "weight": 1,
        }
    ]
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    (target / "checklist.json").write_bytes(encoded)

    receipt = load_current_task_gt_supervision(_Config(root), task_id="Life_005")

    assert receipt["feedback_class"] == "HARD_GT"
    assert receipt["global_feedback"] == {}
    assert receipt["ground_truth_sha256"] == hashlib.sha256(encoded).hexdigest()
    assert receipt["judge_feedback_included"] is False
    assert receipt["task_local_feedback"]["ground_truth_entries"] == payload
    assert "judge" not in receipt["task_local_feedback"]
