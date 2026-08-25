from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from openevo_chemcrow.models import TaskItem
from openevo_chemcrow.paper_evaluator import extract_historical_answers
from openevo_chemcrow.paper_human_review import (
    PAPER_HUMAN_COMPARISON_COUNT,
    PAPER_HUMAN_REVIEWER_COUNT,
    build_paper_human_review_bundle,
    load_or_create_human_review_secret,
    validate_paper_human_review_response,
    write_paper_human_review_bundle,
)


def _tasks() -> list[TaskItem]:
    path = Path(__file__).resolve().parents[1] / "tasks.jsonl"
    return [TaskItem.model_validate_json(line) for line in path.read_text().splitlines()]


def _pairs(tasks: list[TaskItem]) -> dict[str, SimpleNamespace]:
    return {
        task.task_id: SimpleNamespace(
            baseline=SimpleNamespace(answer=f"Current response one for {task.task_id}."),
            evolved=SimpleNamespace(answer=f"Current response two for {task.task_id}."),
        )
        for task in tasks
    }


def test_official_human_bundle_freezes_published_scale_and_blinding(runs_root):
    tasks = _tasks()
    bundle = build_paper_human_review_bundle(
        tasks=tasks,
        pairs=_pairs(tasks),  # type: ignore[arg-type]
        historical=extract_historical_answers(runs_root=runs_root),
        randomization_secret="1" * 64,
    )

    assert bundle["comparison_count"] == PAPER_HUMAN_COMPARISON_COUNT == 42
    assert bundle["required_independent_reviewer_count"] == PAPER_HUMAN_REVIEWER_COUNT == 4
    assert bundle["required_completed_review_count"] == 168
    assert bundle["score_minimum"] == 0
    assert bundle["score_maximum"] == 10
    assert [item["key"] for item in bundle["dimensions"]] == [
        "chemically_accurate",
        "quality_of_reasoning",
        "task_completed",
    ]
    assert {item["comparison"] for item in bundle["private_mappings"]} == {
        "historical_control",
        "baseline",
        "evolved",
    }
    for packet in bundle["packets"]:
        assert packet["condition_identity_included"] is False
        assert packet["trajectory_included"] is False
        assert packet["final_response_only"] is True
        assert packet["packet_id"].startswith("human-")
        assert "chemcrow-" not in packet["packet_id"]
        public_text = json.dumps(packet, sort_keys=True)
        assert "openevo_baseline" not in public_text
        assert "openevo_evolved" not in public_text
        assert "historical_chemcrow" not in public_text
        assert "historical_gpt4" not in public_text


def test_human_bundle_writes_private_packets_and_four_review_forms(tmp_path, runs_root):
    tasks = _tasks()
    bundle = build_paper_human_review_bundle(
        tasks=tasks,
        pairs=_pairs(tasks),  # type: ignore[arg-type]
        historical=extract_historical_answers(runs_root=runs_root),
        randomization_secret="2" * 64,
    )
    manifest = write_paper_human_review_bundle(bundle=bundle, output_root=tmp_path)

    assert manifest["status"] == "READY_FOR_FOUR_EXPERT_REVIEWERS"
    assert len(list((tmp_path / "packets").glob("*.blinded.json"))) == 42
    assert len(list((tmp_path / "responses").glob("*.json"))) == 168
    assert (tmp_path / "private" / "mapping.json").stat().st_mode & 0o777 == 0o600
    assert "response_a" not in json.dumps(manifest)
    assert "response_b" not in json.dumps(manifest)


def test_human_review_randomization_secret_is_private_stable_and_mode_0600(tmp_path):
    first = load_or_create_human_review_secret(tmp_path)
    second = load_or_create_human_review_secret(tmp_path)

    assert first == second
    assert len(first) == 64
    secret_path = tmp_path / "private" / "randomization-secret.txt"
    assert secret_path.stat().st_mode & 0o777 == 0o600


def test_completed_human_review_response_requires_official_zero_to_ten_scale():
    payload = {
        "packet_id": "human-chemcrow-01-c01",
        "reviewer_slot": 1,
        "scores_a": {
            "chemically_accurate": 10,
            "quality_of_reasoning": 8.5,
            "task_completed": 9,
        },
        "scores_b": {
            "chemically_accurate": 7,
            "quality_of_reasoning": 8,
            "task_completed": 6,
        },
        "preferred_response": "A",
        "confidence": 0.8,
        "strengths_a": [],
        "weaknesses_a": [],
        "strengths_b": [],
        "weaknesses_b": [],
        "comments": "",
    }
    response = validate_paper_human_review_response(json.dumps(payload))
    assert response.scores_a.chemically_accurate == 10
    payload["scores_a"]["chemically_accurate"] = 10.1
    with pytest.raises(ValueError):
        validate_paper_human_review_response(json.dumps(payload))
