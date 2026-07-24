from __future__ import annotations

import hashlib

import pytest

from openevo_chembench.taskwise_reporting_v1 import (
    CANARY9_LABELS,
    PROTOCOL_LABELS,
    PrivateTaskwiseRoundResultV1,
    TaskwiseCanaryEvidenceV1,
    build_taskwise_canary9_report_v1,
    build_taskwise_online_report_v1,
)


def _uid(index: int) -> str:
    return hashlib.sha256(f"report-task-{index}".encode()).hexdigest()


def _rows(
    *,
    arm: str,
    predictions: tuple[tuple[str, str, str], ...],
) -> tuple[PrivateTaskwiseRoundResultV1, ...]:
    rows: list[PrivateTaskwiseRoundResultV1] = []
    for task_index, task_predictions in enumerate(predictions):
        for round_index, prediction in enumerate(task_predictions):
            online_memory = arm == "online" and (round_index > 0 or task_index > 0)
            rows.append(
                PrivateTaskwiseRoundResultV1(
                    task_uid=_uid(task_index),
                    task_index=task_index,
                    category="Name_Conversion",
                    arm=arm,
                    round_index=round_index,
                    target="A",
                    official_prediction=prediction,
                    official_parsed=True,
                    strict_parsed=True,
                    core_artifact_id=(
                        f"artifact_{task_index}_{round_index}" if online_memory else None
                    ),
                    memory_digest=(
                        hashlib.sha256(f"memory-{task_index}-{round_index}".encode()).hexdigest()
                        if online_memory
                        else None
                    ),
                )
            )
    return tuple(rows)


def test_taskwise_report_is_prominently_nonstandard_and_uses_round_two() -> None:
    control = _rows(
        arm="control",
        predictions=(("B", "B", "B"), ("A", "B", "A")),
    )
    online = _rows(
        arm="online",
        predictions=(("B", "A", "A"), ("A", "B", "B")),
    )

    report = build_taskwise_online_report_v1(
        control=control,
        online=online,
        online_artifact_validation_failures=1,
        online_security_violations=1,
    )

    assert report["protocol_id"] == "taskwise_online_evolution_v1"
    assert report["protocol_labels"] == list(PROTOCOL_LABELS)
    assert set(PROTOCOL_LABELS) == {
        "ONLINE_TASKWISE_EVOLUTION",
        "TEST_TIME_ADAPTATION",
        "NON_STANDARD_CHEMBENCH4K_PROTOCOL",
        "NOT_A_STANDARD_LEADERBOARD_SCORE",
    }
    assert report["standard_chembench4k_score_claimed"] is False
    assert report["selection_policy"] == "fixed_round_2_no_best_of_no_early_stop"
    assert report["paired_final_round"]["comparison"] == ("online_round_2_vs_control_round_2")
    assert report["control"]["round_2_accuracy"] == 0.5
    assert report["online"]["round_2_accuracy"] == 0.5
    assert report["online"]["wrong_to_correct"] == 1
    assert report["online"]["correct_to_wrong"] == 1
    assert report["online"]["within_task_gain_0_to_1"] == 0.0
    assert report["online"]["within_task_gain_1_to_2"] == 0.0
    assert report["online"]["total_within_task_gain"] == 0.0
    assert report["online"]["artifact_validation_failure_rate"] == 0.25
    assert report["online"]["security_violation_rate"] == 1 / 6
    assert len(report["online"]["cumulative_round_0_accuracy"]) == 2
    assert report["online"]["memory_chain_length"] == 5


def test_per_task_report_contains_fixed_round_predictions_gains_and_memory_chain() -> None:
    control = _rows(arm="control", predictions=(("B", "B", "A"),))
    online = _rows(arm="online", predictions=(("B", "A", "A"),))

    report = build_taskwise_online_report_v1(control=control, online=online)
    task = report["per_task"][0]

    assert task["control"] == {
        "round_0_prediction": "B",
        "round_1_prediction": "B",
        "round_2_prediction": "A",
        "round_0_score": 0.0,
        "round_1_score": 0.0,
        "round_2_score": 1.0,
        "round_0_to_1_gain": 0.0,
        "round_1_to_2_gain": 1.0,
        "wrong_to_correct": True,
        "correct_to_wrong": False,
        "artifact_ids": [],
        "memory_digests": [],
    }
    assert task["online"]["round_0_prediction"] == "B"
    assert task["online"]["round_1_prediction"] == "A"
    assert task["online"]["round_2_prediction"] == "A"
    assert task["online"]["round_0_score"] == 0.0
    assert task["online"]["round_1_score"] == 1.0
    assert task["online"]["round_2_score"] == 1.0
    assert task["online"]["round_0_to_1_gain"] == 1.0
    assert task["online"]["round_1_to_2_gain"] == 0.0
    assert task["online"]["wrong_to_correct"] is True
    assert task["online"]["correct_to_wrong"] is False
    assert len(task["online"]["artifact_ids"]) == 2
    assert len(task["online"]["memory_digests"]) == 2
    assert not any(
        forbidden in key
        for key in report
        for forbidden in ("official_score", "formal_score", "leaderboard_score")
    )


def test_no_uppercase_prediction_remains_an_official_parse_failure() -> None:
    row = PrivateTaskwiseRoundResultV1(
        task_uid=_uid(0),
        task_index=0,
        category="Name_Conversion",
        arm="control",
        round_index=0,
        target="A",
        official_prediction="",
        official_parsed=False,
        strict_parsed=False,
    )

    assert row.correct is False


def _canary_rows(*, arm: str) -> tuple[PrivateTaskwiseRoundResultV1, ...]:
    rows: list[PrivateTaskwiseRoundResultV1] = []
    prior_artifact: str | None = None
    prior_memory: str | None = None
    for task_index in range(9):
        update_1_artifact = f"artifact_canary_{task_index}_1"
        update_2_artifact = f"artifact_canary_{task_index}_2"
        update_1_memory = hashlib.sha256(f"canary-memory-{task_index}-1".encode()).hexdigest()
        update_2_memory = hashlib.sha256(f"canary-memory-{task_index}-2".encode()).hexdigest()
        memories = (
            (prior_artifact, prior_memory),
            (update_1_artifact, update_1_memory),
            (update_2_artifact, update_2_memory),
        )
        for round_index, (artifact_id, memory_digest) in enumerate(memories):
            if arm == "control":
                artifact_id = None
                memory_digest = None
            rows.append(
                PrivateTaskwiseRoundResultV1(
                    task_uid=_uid(task_index),
                    task_index=task_index,
                    category="Name_Conversion",
                    arm=arm,
                    round_index=round_index,
                    target="A",
                    official_prediction="A",
                    official_parsed=True,
                    strict_parsed=True,
                    core_artifact_id=artifact_id,
                    memory_digest=memory_digest,
                )
            )
        prior_artifact = update_2_artifact
        prior_memory = update_2_memory
    return tuple(rows)


def _canary_evidence(**changes: int) -> TaskwiseCanaryEvidenceV1:
    values = {
        "control_completion_count": 27,
        "online_completion_count": 27,
        "control_core_job_count": 0,
        "control_core_artifact_count": 0,
        "online_core_job_count": 18,
        "online_core_artifact_count": 18,
        "control_context_binding_violations": 0,
        "online_context_binding_violations": 0,
        "control_security_violations": 0,
        "online_security_violations": 0,
        "online_artifact_validation_failures": 0,
    }
    values.update(changes)
    return TaskwiseCanaryEvidenceV1(**values)


def test_canary9_is_a_mechanism_security_gate_not_a_performance_gate() -> None:
    report = build_taskwise_canary9_report_v1(
        control=_canary_rows(arm="control"),
        online=_canary_rows(arm="online"),
        evidence=_canary_evidence(),
    )

    gate = report["canary9_gate"]
    assert gate["labels"] == list(CANARY9_LABELS)
    assert gate["performance_gate_applied"] is False
    assert gate["performance_tuning_permitted"] is False
    assert gate["passed"] is True
    assert gate["finding_codes"] == []
    assert report["canary_scope"] == "canary9"
    assert report["canary_labels"] == list(CANARY9_LABELS)
    assert gate["thresholds"] == {
        "tasks_per_arm": 9,
        "completions_per_arm": 27,
        "control_core_jobs": 0,
        "control_core_artifacts": 0,
        "online_core_jobs": 18,
        "online_core_artifacts": 18,
        "context_binding_violations": 0,
        "security_violations": 0,
        "artifact_validation_failures": 0,
    }
    assert report["standard_chembench4k_score_claimed"] is False


@pytest.mark.parametrize(
    ("changes", "finding"),
    (
        ({"control_core_job_count": 1}, "CONTROL_CORE_JOB_PRESENT"),
        ({"control_core_artifact_count": 1}, "CONTROL_CORE_ARTIFACT_PRESENT"),
        (
            {"online_context_binding_violations": 1},
            "CONTEXT_BINDING_VIOLATION_PRESENT",
        ),
        ({"online_security_violations": 1}, "SECURITY_VIOLATION_PRESENT"),
        (
            {"online_artifact_validation_failures": 1},
            "ARTIFACT_VALIDATION_FAILURE_PRESENT",
        ),
    ),
)
def test_canary9_gate_fails_closed_on_mechanism_or_security_evidence(
    changes: dict[str, int],
    finding: str,
) -> None:
    report = build_taskwise_canary9_report_v1(
        control=_canary_rows(arm="control"),
        online=_canary_rows(arm="online"),
        evidence=_canary_evidence(**changes),
    )

    assert report["canary9_gate"]["passed"] is False
    assert finding in report["canary9_gate"]["finding_codes"]
