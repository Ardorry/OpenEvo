from __future__ import annotations

import json
import re
from pathlib import Path

from openevo_chemcrow.hashing import canonical_sha256
from openevo_chemcrow.paper_evaluator import FROZEN_PAPER_TASK_IDS
from openevo_chemcrow.three_artifact_models import THREE_ARTIFACT_ORDER

EVIDENCE_PATH = (
    Path(__file__).resolve().parents[1]
    / "reports/full_v8_public_20260831/per_task_evidence.json"
)


def test_public_per_task_evidence_is_sanitized_and_self_bound() -> None:
    payload = json.loads(EVIDENCE_PATH.read_text(encoding="utf-8"))
    evidence_id = payload.pop("evidence_id")

    assert evidence_id == canonical_sha256(payload)
    assert payload["task_ids"] == list(FROZEN_PAPER_TASK_IDS)
    assert [item["task_id"] for item in payload["results"]] == list(
        FROZEN_PAPER_TASK_IDS
    )
    assert payload["task_count"] == 14
    assert payload["artifact_count"] == 42
    assert payload["raw_answers_included"] is False
    assert payload["task_prompts_included"] is False
    assert payload["tool_observations_included"] is False
    assert payload["host_paths_included"] is False
    assert payload["secrets_included"] is False
    assert all(
        [artifact["artifact_type"] for artifact in row["artifacts"]]
        == [kind.value for kind in THREE_ARTIFACT_ORDER]
        for row in payload["results"]
    )
    rendered = json.dumps(payload, sort_keys=True)
    assert "/home/" not in rendered
    assert re.search(r"\bsk-[A-Za-z0-9]{20,}\b", rendered) is None
