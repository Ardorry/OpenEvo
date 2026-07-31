from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator


PROJECT_ROOT = Path(__file__).resolve().parents[4]
PROTOCOL_ROOT = PROJECT_ROOT / "experiments/sequential_task_reflector_evolution_v0/protocol"


def _schema(name: str) -> dict:
    payload = json.loads((PROTOCOL_ROOT / name).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(payload)
    return payload


def test_all_protocol_json_schemas_are_valid() -> None:
    for name in (
        "attempt_manifest.schema.json",
        "composite_manifest.schema.json",
        "experiment_state.schema.json",
        "feedback_allowlist.schema.json",
        "reflection_proposal.schema.json",
        "native_cycle_manifest.schema.json",
    ):
        _schema(name)


def test_initial_state_validates_against_schema() -> None:
    state = json.loads(
        (PROJECT_ROOT / "experiments/sequential_task_reflector_evolution_v0/manifests/experiment_state.json").read_text(encoding="utf-8")
    )
    Draft202012Validator(_schema("experiment_state.schema.json")).validate(state)


def test_reflection_schema_requires_joint_review_sections() -> None:
    proposal = {
        "source_task_id": "Life_005",
        "source_attempt_id": "Life_005_a0",
        "parent_composite_revision": "c000",
        "observed_score": 20,
        "best_score_on_current_task": 20,
        "agent_system": {"action": "keep", "reason": "Insufficient general evidence", "proposed_revision": "as000"},
        "text_memory": {"action": "update", "reason": "General debugging evidence", "proposed_revision": "tm001"},
        "skill_bundle": {"action": "keep", "reason": "No repeated operation", "add": [], "modify": [], "remove": [], "proposed_revision": "sk000"},
        "expected_generalization": "Safer debugging in future public tasks",
        "task_specific_content_removed": [],
        "admission_required": True
    }
    Draft202012Validator(_schema("reflection_proposal.schema.json")).validate(proposal)
