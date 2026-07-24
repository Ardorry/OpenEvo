from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from openevo_chembench.chembench4k_models import CHEMBENCH4K_CATEGORIES
from openevo_chembench.taskwise_reporting_v1 import PrivateTaskwiseRoundResultV1
from openevo_chembench.taskwise_sampling_v1 import PILOT500_STREAM_SCOPES
from openevo_chembench.taskwise_stream_statistics_v1 import (
    STREAM_BOOTSTRAP_SAMPLES,
    STREAM_BOOTSTRAP_SEED,
    PrivateTaskwisePairedStreamV1,
    build_taskwise_pilot500_stream_report_v1,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _arm_rows(
    *,
    stream_index: int,
    arm: str,
    prediction: str,
) -> tuple[PrivateTaskwiseRoundResultV1, ...]:
    rows: list[PrivateTaskwiseRoundResultV1] = []
    prior_artifact: str | None = None
    prior_memory: str | None = None
    for task_index in range(50):
        uid = _digest(f"stream-{stream_index}-task-{task_index}")
        category = CHEMBENCH4K_CATEGORIES[
            (stream_index * 50 + task_index) % len(CHEMBENCH4K_CATEGORIES)
        ]
        for round_index in (0, 1, 2):
            round_prediction = "B" if arm == "online" and round_index == 0 else prediction
            if arm == "control":
                artifact = None
                memory = None
            elif round_index == 0:
                artifact = prior_artifact
                memory = prior_memory
            else:
                artifact = f"artifact_{stream_index}_{task_index}_{round_index}"
                memory = _digest(f"memory-{stream_index}-{task_index}-{round_index}")
            rows.append(
                PrivateTaskwiseRoundResultV1(
                    task_uid=uid,
                    task_index=task_index,
                    category=category,
                    arm=arm,  # type: ignore[arg-type]
                    round_index=round_index,  # type: ignore[arg-type]
                    target="A",
                    official_prediction=round_prediction,
                    official_parsed=True,
                    strict_parsed=True,
                    core_artifact_id=artifact,
                    memory_digest=memory,
                )
            )
        if arm == "online":
            prior_artifact = f"artifact_{stream_index}_{task_index}_2"
            prior_memory = _digest(f"memory-{stream_index}-{task_index}-2")
    return tuple(rows)


def _streams(
    *,
    violation_stream: int | None = None,
) -> tuple[PrivateTaskwisePairedStreamV1, ...]:
    return tuple(
        PrivateTaskwisePairedStreamV1(
            stream_id=scope,
            control=_arm_rows(
                stream_index=index,
                arm="control",
                prediction="B",
            ),
            online=_arm_rows(
                stream_index=index,
                arm="online",
                prediction="A",
            ),
            online_security_violations=int(index == violation_stream),
            approved_memory_utf8_bytes=tuple(100 + index + update for update in range(100)),
        )
        for index, scope in enumerate(PILOT500_STREAM_SCOPES)
    )


def test_stream_statistics_use_ten_independent_chains_and_are_deterministic() -> None:
    first = build_taskwise_pilot500_stream_report_v1(_streams())
    second = build_taskwise_pilot500_stream_report_v1(_streams())

    assert first == second
    assert first["stream_count"] == 10
    assert first["tasks_per_stream"] == 50
    assert first["task_count"] == 500
    assert first["status"] == "COMPLETED"
    assert first["inference_unit"] == "independent_task_stream"
    assert first["paired_final_round"]["control_round_2_accuracy"] == 0.0
    assert first["paired_final_round"]["online_round_2_accuracy"] == 1.0
    assert first["paired_final_round"]["absolute_delta"] == 1.0
    assert first["paired_final_round"]["stream_block_bootstrap_95_ci"] == {
        "lower": 1.0,
        "upper": 1.0,
        "samples": STREAM_BOOTSTRAP_SAMPLES,
        "seed": STREAM_BOOTSTRAP_SEED,
    }
    assert (
        first["dependent_task_observations"]["classification"]
        == "DESCRIPTIVE_ONLY_DEPENDENT_TASK_OBSERVATIONS"
    )
    assert (
        len(first["dependent_task_observations"]["round_0_accuracy_by_within_stream_position"])
        == 50
    )
    assert all(item["online_memory_chain_length"] == 100 for item in first["per_stream"])
    assert all(item["round_2_delta_vs_control"] == 1.0 for item in first["per_stream"])
    assert all(item["round_2_absolute_delta"] == 1.0 for item in first["per_stream"])
    assert all(item["wrong_to_correct_0_to_1"] == 50 for item in first["per_stream"])
    assert all(item["wrong_to_correct_0_to_2"] == 50 for item in first["per_stream"])
    assert all(item["memory_growth"]["delta_utf8_bytes"] == 99 for item in first["per_stream"])
    assert first["paired_final_round"]["positive_stream_count"] == 10
    assert first["paired_final_round"]["stream_delta_mean"] == 1.0
    assert first["paired_final_round"]["stream_delta_median"] == 1.0
    assert first["paired_final_round"]["stream_delta_std"] == 0.0
    assert first["paired_final_round"]["stream_exact_sign_flip_p_value"] > 0.0
    assert first["completed_streams"] == 10
    assert first["go_gate"] == {
        "completed_streams_required": 10,
        "stream_count_required": 10,
        "task_count_required": 500,
        "security_violations_required": 0,
        "artifact_validation_failures_required": 0,
        "context_binding_violations_required": 0,
        "mean_stream_delta_minimum": 0.02,
        "stream_block_bootstrap_ci_lower_must_exceed": 0.0,
        "positive_stream_count_minimum": 7,
    }
    assert first["decision"] == "GO"


def test_stream_security_violation_forces_no_go() -> None:
    report = build_taskwise_pilot500_stream_report_v1(_streams(violation_stream=3))

    assert report["security_violations"] == 1
    assert report["decision"] == "NO_GO"


def test_stream_context_or_completion_failure_forces_no_go() -> None:
    streams = list(_streams())
    original = streams[2]
    streams[2] = PrivateTaskwisePairedStreamV1(
        stream_id=original.stream_id,
        control=original.control,
        online=original.online,
        context_binding_violations=1,
        completed=False,
        approved_memory_utf8_bytes=original.approved_memory_utf8_bytes,
    )
    report = build_taskwise_pilot500_stream_report_v1(tuple(streams))

    assert report["completed_streams"] == 9
    assert report["context_binding_violations"] == 1
    assert report["decision"] == "NO_GO"


def test_stream_report_rejects_cross_stream_task_reuse() -> None:
    streams = list(_streams())
    streams[1] = PrivateTaskwisePairedStreamV1(
        stream_id=streams[1].stream_id,
        control=streams[0].control,
        online=streams[0].online,
    )

    try:
        build_taskwise_pilot500_stream_report_v1(tuple(streams))
    except ValueError as exc:
        assert "must not share task UIDs" in str(exc)
    else:  # pragma: no cover - fail closed assertion
        raise AssertionError("cross-stream task reuse was accepted")


def test_stream_report_inherits_cross_arm_private_pairing_gate() -> None:
    streams = list(_streams())
    original = streams[0]
    first_uid = original.online[0].task_uid
    mismatched_online = tuple(
        replace(row, target="B") if row.task_uid == first_uid else row for row in original.online
    )
    streams[0] = replace(original, online=mismatched_online)

    with pytest.raises(ValueError, match="paired task identity"):
        build_taskwise_pilot500_stream_report_v1(tuple(streams))
