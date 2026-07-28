from __future__ import annotations

import json
from pathlib import Path

import pytest

from openevo_researchclawbench.config import (
    ARTIFACT_TYPES,
    EXPECTED_ARTIFACT_EVOLUTION_REQUESTS,
    EXPECTED_CANDIDATE_RUNS,
    EXPECTED_REFLECTOR_CYCLES,
    FROZEN_TASKS,
)
from openevo_researchclawbench.contamination_audit import run_offline_contamination_audit
from openevo_researchclawbench.evolution_loop import (
    FrozenTrainingSchedule,
    official_freeze_configuration,
    reject_official_training_transition,
)
from openevo_researchclawbench.freeze_gate import (
    freeze_community_artifacts,
    validate_official_run_set,
)
from openevo_researchclawbench.hard_gt_teacher import (
    TeacherPrivateResult,
    assert_overlay_transition,
    build_task_local_overlay,
    native_teacher_attachment_capability,
    require_global_artifact_clean,
    scan_global_artifact,
)


def _teacher() -> TeacherPrivateResult:
    return TeacherPrivateResult.from_private_payload({
        "task_id": "Life_005",
        "attempt_id": "Life_005_a0",
        "mode": "HARD_GT",
        "corrections": ["Use the verified current-task result"],
        "correct_values": ["3.141592"],
        "tolerances": ["0.001"],
        "expected_structure": ["table and plot"],
        "failed_tests": ["result field missing"],
        "missing_fields": ["uncertainty"],
    })


def test_frozen_training_schedule_has_51_attempts() -> None:
    assert len(FrozenTrainingSchedule.build().attempts) == EXPECTED_CANDIDATE_RUNS == 51


def test_frozen_training_schedule_has_34_cycles() -> None:
    requests = FrozenTrainingSchedule.build().evolution_requests
    assert len({(item.task_id, item.reflector_round) for item in requests}) == EXPECTED_REFLECTOR_CYCLES == 34


def test_frozen_training_schedule_has_102_artifact_requests() -> None:
    assert len(FrozenTrainingSchedule.build().evolution_requests) == EXPECTED_ARTIFACT_EVOLUTION_REQUESTS == 102


def test_each_cycle_reviews_all_three_artifacts() -> None:
    requests = FrozenTrainingSchedule.build().evolution_requests
    for task_id in FROZEN_TASKS:
        for round_index in (0, 1):
            assert {
                item.artifact_type for item in requests
                if item.task_id == task_id and item.reflector_round == round_index
            } == set(ARTIFACT_TYPES)


def test_task_local_teacher_overlay_can_contain_current_truth() -> None:
    overlay = build_task_local_overlay(_teacher())
    assert overlay.task_id == "Life_005"
    assert "3.141592" in overlay.text


def test_task_local_overlay_cannot_cross_task() -> None:
    with pytest.raises(RuntimeError, match="CROSS_TASK"):
        assert_overlay_transition(build_task_local_overlay(_teacher()), "Life_006")


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("Life_005 is reusable", "TASK_ID_LITERAL"),
        ("read /home/user/private/file", "ABSOLUTE_PATH"),
        ("inspect checklist details", "EVALUATOR_TERM"),
        ("paper DOI 10.1234/ABC.1", "TARGET_DOI"),
        ("the value is 3.141592", "HIGH_PRECISION_VALUE"),
    ],
)
def test_global_artifact_scanner_rejects_teacher_leaks(text: str, reason: str) -> None:
    scan = scan_global_artifact(text)
    assert not scan.passed and reason in scan.reasons
    with pytest.raises(ValueError, match="GLOBAL_ARTIFACT"):
        require_global_artifact_clean(scan)


def test_global_artifact_scanner_accepts_abstract_procedure() -> None:
    scan = scan_global_artifact(
        "Validate schemas, establish baselines, quantify uncertainty, and trace conclusions to outputs."
    )
    assert scan.passed


def test_native_teacher_feedback_attachment_uses_core_authority() -> None:
    receipt = native_teacher_attachment_capability(Path(__file__).resolve().parents[3])
    assert receipt["post_run_feedback_attachment_to_native_dataset"] is True
    assert receipt["session_completed_immutable"] is True
    assert receipt["evaluator_authority"] == "CORE_CONTROL_SERVICE_IDENTITY"
    assert receipt["durable_cross_process_store"] is True
    assert receipt["production_evolution_http_transport"] is True
    assert receipt["production_science_successor_hook"] is True


@pytest.mark.parametrize("transition", ["evolution", "reflector", "teacher", "feedback", "cross_task_update"])
def test_official_freeze_rejects_training_transitions(transition: str) -> None:
    config = official_freeze_configuration("composite-final")
    with pytest.raises(RuntimeError, match="OFFICIAL_FROZEN_TRANSITION_REJECTED"):
        reject_official_training_transition(config, transition)


def test_freeze_manifest_closes_model_runtime_and_artifact_identity() -> None:
    payload = {
        "agent_system_artifact_id": "artifact-as",
        "agent_system_sha256": "1" * 64,
        "text_memory_artifact_id": "artifact-tm",
        "text_memory_sha256": "2" * 64,
        "skill_bundle_artifact_id": "artifact-sk",
        "skill_bundle_sha256": "3" * 64,
        "composite_sha256": "4" * 64,
        "openevo_commit": "5" * 40,
        "adapter_tree_sha256": "6" * 64,
        "managed_runtime_digest": "7" * 64,
        "codex_cli_version": "0.144.1",
        "candidate_model": "gpt-5.5",
        "reasoning_level": "high",
        "tool_policy_sha256": "8" * 64,
        "budget_policy_sha256": "9" * 64,
        "contamination_receipt_sha256": "a" * 64,
    }
    receipt = freeze_community_artifacts(payload)
    assert receipt.manifest["evolution_enabled"] is False
    assert receipt.manifest["official_candidate_run_count"] == 40


def test_official_scorer_is_withheld_before_all_40_runs() -> None:
    with pytest.raises(RuntimeError, match="WITHHELD"):
        validate_official_run_set([], frozen_protocol_sha256="a" * 64)


def _task(root: Path, task_id: str, paper: bytes, data: bytes | None = None) -> None:
    task = root / task_id
    (task / "data").mkdir(parents=True)
    (task / "related_work").mkdir()
    (task / "target_study").mkdir()
    (task / "task_info.json").write_text(
        json.dumps({"task": f"distinct public description {task_id}", "data": []}),
        encoding="utf-8",
    )
    (task / "target_study" / "paper.pdf").write_bytes(paper)
    if data is not None:
        (task / "data" / "public.csv").write_bytes(data)


def test_contamination_audit_hashes_opaque_target_without_parsing(tmp_path: Path) -> None:
    for index, task_id in enumerate(FROZEN_TASKS):
        _task(tmp_path, task_id, f"community-{index}".encode())
    for index in range(40):
        _task(tmp_path, f"Official_{index:03d}", f"official-{index}".encode())
    receipt = run_offline_contamination_audit(tmp_path)
    assert receipt["status"] == "PASS"
    assert receipt["target_study_handling"]["paper_content_opened_or_parsed"] is False
    assert receipt["target_study_handling"]["checklist_opened"] is False


def test_contamination_audit_blocks_exact_cross_cohort_paper(tmp_path: Path) -> None:
    for index, task_id in enumerate(FROZEN_TASKS):
        _task(tmp_path, task_id, b"duplicate" if index == 0 else f"community-{index}".encode())
    for index in range(40):
        _task(tmp_path, f"Official_{index:03d}", b"duplicate" if index == 0 else f"official-{index}".encode())
    receipt = run_offline_contamination_audit(tmp_path)
    assert receipt["status"] == "COMMUNITY_OFFICIAL_CONTAMINATION_RISK"
    assert receipt["model_execution_allowed"] is False


def test_contamination_audit_blocks_normalized_near_duplicate_data(tmp_path: Path) -> None:
    for index, task_id in enumerate(FROZEN_TASKS):
        data = b"x,y\n1,2\n" if index == 0 else None
        _task(tmp_path, task_id, f"community-{index}".encode(), data)
    for index in range(40):
        data = b"x, y\n 1, 2\n" if index == 0 else None
        _task(tmp_path, f"Official_{index:03d}", f"official-{index}".encode(), data)
    receipt = run_offline_contamination_audit(tmp_path)
    assert receipt["status"] == "COMMUNITY_OFFICIAL_CONTAMINATION_RISK"
    assert receipt["findings"]["conservative_near_data_file_matches"]
