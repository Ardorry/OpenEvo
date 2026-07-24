"""Private aggregation for the non-standard taskwise online-recovery protocol."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from openevo_chembench.chembench4k_models import CHEMBENCH4K_CATEGORIES


PROTOCOL_ID = "taskwise_online_evolution_v1"
PROTOCOL_LABELS = (
    "ONLINE_TASKWISE_EVOLUTION",
    "TEST_TIME_ADAPTATION",
    "NON_STANDARD_CHEMBENCH4K_PROTOCOL",
    "NOT_A_STANDARD_LEADERBOARD_SCORE",
)
CANARY9_LABELS = (
    "NON_PERFORMANCE_SAFETY_CANARY",
    "MECHANISM_AND_SECURITY_VALIDATION_ONLY",
    "DO_NOT_USE_FOR_ARTIFACT_OR_PROMPT_TUNING",
)
_ARMS = ("control", "online")
_ROUNDS = (0, 1, 2)


@dataclass(frozen=True, slots=True, repr=False)
class PrivateTaskwiseRoundResultV1:
    """One target-bearing round result confined to the private reporting plane."""

    task_uid: str
    task_index: int
    category: str
    arm: Literal["control", "online"]
    round_index: Literal[0, 1, 2]
    target: Literal["A", "B", "C", "D"]
    official_prediction: str
    official_parsed: bool
    strict_parsed: bool
    core_artifact_id: str | None = None
    memory_digest: str | None = None

    def __repr__(self) -> str:
        return "PrivateTaskwiseRoundResultV1(<redacted>)"

    __str__ = __repr__

    def __post_init__(self) -> None:
        if (
            type(self.task_uid) is not str
            or len(self.task_uid) != 64
            or any(character not in "0123456789abcdef" for character in self.task_uid)
        ):
            raise ValueError("task_uid must be a lowercase SHA-256 digest")
        if self.category not in CHEMBENCH4K_CATEGORIES:
            raise ValueError("category is not a frozen ChemBench4K category")
        if (
            isinstance(self.task_index, bool)
            or not isinstance(self.task_index, int)
            or self.task_index < 0
        ):
            raise ValueError("task_index must be non-negative")
        if self.arm not in _ARMS:
            raise ValueError("arm must be control or online")
        if self.round_index not in _ROUNDS:
            raise ValueError("round_index must be 0, 1, or 2")
        if self.target not in {"A", "B", "C", "D"}:
            raise ValueError("private target must be A/B/C/D")
        if type(self.official_prediction) is not str or (
            self.official_prediction
            and (len(self.official_prediction) != 1 or not self.official_prediction.isupper())
        ):
            raise ValueError("official_prediction must be empty or one uppercase character")
        if type(self.official_parsed) is not bool or type(self.strict_parsed) is not bool:
            raise TypeError("parse indicators must be booleans")
        if self.official_parsed != bool(self.official_prediction):
            raise ValueError("official parse indicator does not match prediction")
        if (self.core_artifact_id is None) != (self.memory_digest is None):
            raise ValueError("artifact identity and memory digest must appear together")
        if self.core_artifact_id is not None and (
            type(self.core_artifact_id) is not str
            or not self.core_artifact_id
            or "/" in self.core_artifact_id
            or "\\" in self.core_artifact_id
        ):
            raise ValueError("core_artifact_id must be a non-path identifier or None")
        if self.memory_digest is not None and (
            type(self.memory_digest) is not str
            or len(self.memory_digest) != 64
            or any(character not in "0123456789abcdef" for character in self.memory_digest)
        ):
            raise ValueError("memory_digest must be a lowercase SHA-256 or None")

    @property
    def correct(self) -> bool:
        return self.official_prediction == self.target


@dataclass(frozen=True, slots=True)
class TaskwiseCanaryEvidenceV1:
    """Content-free mechanism/security counters recomputed from run evidence."""

    control_completion_count: int
    online_completion_count: int
    control_core_job_count: int
    control_core_artifact_count: int
    online_core_job_count: int
    online_core_artifact_count: int
    control_context_binding_violations: int
    online_context_binding_violations: int
    control_security_violations: int
    online_security_violations: int
    online_artifact_validation_failures: int

    def __post_init__(self) -> None:
        for field_name in self.__dataclass_fields__:
            _require_nonnegative_int(getattr(self, field_name), field_name)


def build_taskwise_online_report_v1(
    *,
    control: tuple[PrivateTaskwiseRoundResultV1, ...],
    online: tuple[PrivateTaskwiseRoundResultV1, ...],
    control_security_violations: int = 0,
    online_security_violations: int = 0,
    online_artifact_validation_failures: int = 0,
) -> dict[str, Any]:
    """Aggregate fixed Round-0/1/2 outcomes without claiming an official score."""

    control_by_key = _validate_arm(control, expected_arm="control")
    online_by_key = _validate_arm(online, expected_arm="online")
    if set(control_by_key) != set(online_by_key):
        raise ValueError("control and online arms must contain identical paired tasks")

    _require_nonnegative_int(
        control_security_violations,
        "control_security_violations",
    )
    _require_nonnegative_int(
        online_security_violations,
        "online_security_violations",
    )
    _require_nonnegative_int(
        online_artifact_validation_failures,
        "online_artifact_validation_failures",
    )
    control_metrics = _arm_metrics(
        control_by_key,
        security_violations=control_security_violations,
        artifact_validation_failures=0,
    )
    online_metrics = _arm_metrics(
        online_by_key,
        security_violations=online_security_violations,
        artifact_validation_failures=online_artifact_validation_failures,
    )
    final_pairs = [
        (control_by_key[key][2], online_by_key[key][2]) for key in sorted(control_by_key)
    ]
    control_final_correct = sum(left.correct for left, _right in final_pairs)
    online_final_correct = sum(right.correct for _left, right in final_pairs)
    task_count = len(final_pairs)
    paired_final = {
        "comparison": "online_round_2_vs_control_round_2",
        "control_round_2_accuracy": control_final_correct / task_count,
        "online_round_2_accuracy": online_final_correct / task_count,
        "absolute_delta": (online_final_correct - control_final_correct) / task_count,
        "control_wrong_online_correct": sum(
            (not left.correct) and right.correct for left, right in final_pairs
        ),
        "control_correct_online_wrong": sum(
            left.correct and (not right.correct) for left, right in final_pairs
        ),
        "both_correct": sum(left.correct and right.correct for left, right in final_pairs),
        "both_wrong": sum(
            (not left.correct) and (not right.correct) for left, right in final_pairs
        ),
    }
    return {
        "schema_version": "taskwise_online_evolution_report_v1",
        "protocol_id": PROTOCOL_ID,
        "protocol_labels": list(PROTOCOL_LABELS),
        "standard_chembench4k_score_claimed": False,
        "selection_policy": "fixed_round_2_no_best_of_no_early_stop",
        "task_count": task_count,
        "per_task": _per_task_report(control_by_key, online_by_key),
        "control": control_metrics,
        "online": online_metrics,
        "paired_final_round": paired_final,
    }


def build_taskwise_canary9_report_v1(
    *,
    control: tuple[PrivateTaskwiseRoundResultV1, ...],
    online: tuple[PrivateTaskwiseRoundResultV1, ...],
    evidence: TaskwiseCanaryEvidenceV1,
) -> dict[str, Any]:
    """Build the nine-task mechanism/safety gate without a performance gate."""

    if type(evidence) is not TaskwiseCanaryEvidenceV1:
        raise TypeError("canary evidence must be exact TaskwiseCanaryEvidenceV1")
    control_by_key = _validate_arm(control, expected_arm="control")
    online_by_key = _validate_arm(online, expected_arm="online")
    if set(control_by_key) != set(online_by_key):
        raise ValueError("control and online canary tasks must be exactly paired")
    report = build_taskwise_online_report_v1(
        control=control,
        online=online,
        control_security_violations=evidence.control_security_violations,
        online_security_violations=evidence.online_security_violations,
        online_artifact_validation_failures=evidence.online_artifact_validation_failures,
    )
    findings: list[str] = []
    if len(control_by_key) != 9:
        findings.append("CANARY_TASK_COUNT_INVALID")
    if evidence.control_completion_count != 27:
        findings.append("CONTROL_COMPLETION_BUDGET_INVALID")
    if evidence.online_completion_count != 27:
        findings.append("ONLINE_COMPLETION_BUDGET_INVALID")
    if evidence.control_core_job_count != 0:
        findings.append("CONTROL_CORE_JOB_PRESENT")
    if evidence.control_core_artifact_count != 0:
        findings.append("CONTROL_CORE_ARTIFACT_PRESENT")
    if evidence.online_core_job_count != 18:
        findings.append("ONLINE_CORE_JOB_COUNT_INVALID")
    if evidence.online_core_artifact_count != 18:
        findings.append("ONLINE_CORE_ARTIFACT_COUNT_INVALID")
    if any(
        row.core_artifact_id is not None or row.memory_digest is not None
        for rounds in control_by_key.values()
        for row in rounds.values()
    ):
        findings.append("CONTROL_MEMORY_PRESENT")
    findings.extend(_online_memory_chain_findings(online_by_key))
    if evidence.control_context_binding_violations or evidence.online_context_binding_violations:
        findings.append("CONTEXT_BINDING_VIOLATION_PRESENT")
    if evidence.control_security_violations or evidence.online_security_violations:
        findings.append("SECURITY_VIOLATION_PRESENT")
    if evidence.online_artifact_validation_failures:
        findings.append("ARTIFACT_VALIDATION_FAILURE_PRESENT")

    gate = {
        "schema_version": "taskwise_online_canary9_gate_v1",
        "labels": list(CANARY9_LABELS),
        "performance_gate_applied": False,
        "performance_tuning_permitted": False,
        "thresholds": {
            "tasks_per_arm": 9,
            "completions_per_arm": 27,
            "control_core_jobs": 0,
            "control_core_artifacts": 0,
            "online_core_jobs": 18,
            "online_core_artifacts": 18,
            "context_binding_violations": 0,
            "security_violations": 0,
            "artifact_validation_failures": 0,
        },
        "observed": {
            field_name: getattr(evidence, field_name)
            for field_name in evidence.__dataclass_fields__
        },
        "passed": not findings,
        "finding_codes": sorted(set(findings)),
    }
    report["canary_scope"] = "canary9"
    report["canary_labels"] = list(CANARY9_LABELS)
    report["canary9_gate"] = gate
    return report


def _validate_arm(
    rows: tuple[PrivateTaskwiseRoundResultV1, ...],
    *,
    expected_arm: Literal["control", "online"],
) -> dict[str, dict[int, PrivateTaskwiseRoundResultV1]]:
    if not isinstance(rows, tuple) or not rows:
        raise ValueError(f"{expected_arm} results must be a non-empty tuple")
    grouped: dict[str, dict[int, PrivateTaskwiseRoundResultV1]] = {}
    categories: dict[str, str] = {}
    indices: dict[str, int] = {}
    for row in rows:
        if type(row) is not PrivateTaskwiseRoundResultV1:
            raise TypeError("report rows must be exact PrivateTaskwiseRoundResultV1")
        if row.arm != expected_arm:
            raise ValueError("report row arm does not match its collection")
        if row.task_uid in categories and categories[row.task_uid] != row.category:
            raise ValueError("one task UID cannot belong to multiple categories")
        categories[row.task_uid] = row.category
        if row.task_uid in indices and indices[row.task_uid] != row.task_index:
            raise ValueError("one task UID cannot have multiple stream positions")
        indices[row.task_uid] = row.task_index
        rounds = grouped.setdefault(row.task_uid, {})
        if row.round_index in rounds:
            raise ValueError("taskwise report contains a duplicate round")
        rounds[row.round_index] = row
    if any(set(rounds) != set(_ROUNDS) for rounds in grouped.values()):
        raise ValueError("every task must contain exactly rounds 0, 1, and 2")
    if sorted(indices.values()) != list(range(len(indices))):
        raise ValueError("task indices must be the complete zero-based stream")
    return grouped


def _arm_metrics(
    grouped: dict[str, dict[int, PrivateTaskwiseRoundResultV1]],
    *,
    security_violations: int,
    artifact_validation_failures: int,
) -> dict[str, Any]:
    task_count = len(grouped)
    metrics: dict[str, Any] = {}
    for round_index in _ROUNDS:
        rows = [rounds[round_index] for rounds in grouped.values()]
        metrics[f"round_{round_index}_accuracy"] = sum(row.correct for row in rows) / task_count
        metrics[f"round_{round_index}_official_parse_rate"] = (
            sum(row.official_parsed for row in rows) / task_count
        )
        metrics[f"round_{round_index}_strict_parse_rate"] = (
            sum(row.strict_parsed for row in rows) / task_count
        )

    transitions = [(rounds[0], rounds[2]) for rounds in grouped.values()]
    wrong_to_correct = sum(
        (not round_0.correct) and round_2.correct for round_0, round_2 in transitions
    )
    correct_to_wrong = sum(
        round_0.correct and (not round_2.correct) for round_0, round_2 in transitions
    )
    initially_wrong = sum(not round_0.correct for round_0, _round_2 in transitions)
    initially_correct = task_count - initially_wrong
    metrics.update(
        {
            "final_round": 2,
            "within_task_gain_0_to_1": (
                sum(item[1].correct - item[0].correct for item in grouped.values()) / task_count
            ),
            "within_task_gain_1_to_2": (
                sum(item[2].correct - item[1].correct for item in grouped.values()) / task_count
            ),
            "total_within_task_gain": (
                sum(item[2].correct - item[0].correct for item in grouped.values()) / task_count
            ),
            "wrong_to_correct": wrong_to_correct,
            "correct_to_wrong": correct_to_wrong,
            "wrong_to_correct_rate": (
                wrong_to_correct / initially_wrong if initially_wrong else None
            ),
            "correct_to_wrong_rate": (
                correct_to_wrong / initially_correct if initially_correct else None
            ),
            "parse_change": sum(
                round_0.strict_parsed != round_2.strict_parsed for round_0, round_2 in transitions
            ),
            "next_task_round_0_accuracy_by_stream_position": (
                _round_0_by_stream_position(grouped)
            ),
            "cumulative_round_0_accuracy": _cumulative_round_0_accuracy(grouped),
            "memory_chain_length": _memory_chain_length(grouped),
            "artifact_validation_failure_rate": (artifact_validation_failures / (2 * task_count)),
            "security_violation_rate": (security_violations / (3 * task_count)),
            "per_category_recovery": _per_category_recovery(grouped),
        }
    )
    return metrics


def _per_category_recovery(
    grouped: dict[str, dict[int, PrivateTaskwiseRoundResultV1]],
) -> dict[str, dict[str, int | float | None]]:
    by_category: dict[str, list[tuple[PrivateTaskwiseRoundResultV1, ...]]] = {}
    for rounds in grouped.values():
        rows = tuple(rounds[index] for index in _ROUNDS)
        by_category.setdefault(rows[0].category, []).append(rows)
    result: dict[str, dict[str, int | float | None]] = {}
    for category, rows in sorted(by_category.items()):
        initially_wrong = sum(not item[0].correct for item in rows)
        initially_correct = len(rows) - initially_wrong
        recovered = sum((not item[0].correct) and item[2].correct for item in rows)
        regressed = sum(item[0].correct and (not item[2].correct) for item in rows)
        result[category] = {
            "tasks": len(rows),
            "wrong_to_correct": recovered,
            "correct_to_wrong": regressed,
            "recovery_rate": recovered / initially_wrong if initially_wrong else None,
            "regression_rate": regressed / initially_correct if initially_correct else None,
        }
    return result


def _per_task_report(
    control: dict[str, dict[int, PrivateTaskwiseRoundResultV1]],
    online: dict[str, dict[int, PrivateTaskwiseRoundResultV1]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    ordered_uids = sorted(
        control,
        key=lambda uid: control[uid][0].task_index,
    )
    for uid in ordered_uids:
        control_rounds = control[uid]
        online_rounds = online[uid]
        row: dict[str, Any] = {
            "task_uid": uid,
            "task_index": control_rounds[0].task_index,
            "category": control_rounds[0].category,
            "control": _task_arm_payload(control_rounds),
            "online": _task_arm_payload(online_rounds),
        }
        rows.append(row)
    return rows


def _task_arm_payload(
    rounds: dict[int, PrivateTaskwiseRoundResultV1],
) -> dict[str, object]:
    payload: dict[str, object] = {
        f"round_{index}_prediction": rounds[index].official_prediction for index in _ROUNDS
    }
    payload.update({f"round_{index}_score": float(rounds[index].correct) for index in _ROUNDS})
    payload.update(
        {
            "round_0_to_1_gain": (float(rounds[1].correct) - float(rounds[0].correct)),
            "round_1_to_2_gain": (float(rounds[2].correct) - float(rounds[1].correct)),
            "wrong_to_correct": (not rounds[0].correct and rounds[2].correct),
            "correct_to_wrong": (rounds[0].correct and not rounds[2].correct),
            "artifact_ids": [
                rounds[index].core_artifact_id
                for index in _ROUNDS
                if rounds[index].core_artifact_id is not None
            ],
            "memory_digests": [
                rounds[index].memory_digest
                for index in _ROUNDS
                if rounds[index].memory_digest is not None
            ],
        }
    )
    return payload


def _round_0_by_stream_position(
    grouped: dict[str, dict[int, PrivateTaskwiseRoundResultV1]],
) -> list[dict[str, int | float]]:
    ordered = sorted(grouped.values(), key=lambda rounds: rounds[0].task_index)
    return [
        {
            "stream_position": rounds[0].task_index,
            "round_0_accuracy": float(rounds[0].correct),
        }
        for rounds in ordered
    ]


def _cumulative_round_0_accuracy(
    grouped: dict[str, dict[int, PrivateTaskwiseRoundResultV1]],
) -> list[dict[str, int | float]]:
    ordered = sorted(grouped.values(), key=lambda rounds: rounds[0].task_index)
    correct = 0
    result: list[dict[str, int | float]] = []
    for position, rounds in enumerate(ordered):
        correct += int(rounds[0].correct)
        result.append(
            {
                "stream_position": position,
                "cumulative_accuracy": correct / (position + 1),
            }
        )
    return result


def _memory_chain_length(
    grouped: dict[str, dict[int, PrivateTaskwiseRoundResultV1]],
) -> int:
    artifact_ids: list[str] = []
    for rounds in sorted(grouped.values(), key=lambda item: item[0].task_index):
        for round_index in _ROUNDS:
            artifact_id = rounds[round_index].core_artifact_id
            if artifact_id is not None and artifact_id not in artifact_ids:
                artifact_ids.append(artifact_id)
    return len(artifact_ids)


def _online_memory_chain_findings(
    grouped: dict[str, dict[int, PrivateTaskwiseRoundResultV1]],
) -> list[str]:
    findings: list[str] = []
    ordered = sorted(grouped.values(), key=lambda item: item[0].task_index)
    update_artifact_ids: list[str] = []
    prior_final: PrivateTaskwiseRoundResultV1 | None = None
    for task_index, rounds in enumerate(ordered):
        round_0, round_1, round_2 = (rounds[index] for index in _ROUNDS)
        if task_index == 0:
            if round_0.core_artifact_id is not None or round_0.memory_digest is not None:
                findings.append("ONLINE_GENERATION_ZERO_MEMORY_INVALID")
        elif prior_final is None or (
            round_0.core_artifact_id != prior_final.core_artifact_id
            or round_0.memory_digest != prior_final.memory_digest
        ):
            findings.append("ONLINE_CARRYOVER_BINDING_INVALID")
        for row in (round_1, round_2):
            if row.core_artifact_id is None or row.memory_digest is None:
                findings.append("ONLINE_UPDATE_MEMORY_MISSING")
            else:
                update_artifact_ids.append(row.core_artifact_id)
        if (
            round_1.core_artifact_id is not None
            and round_1.core_artifact_id == round_2.core_artifact_id
        ):
            findings.append("ONLINE_UPDATE_ARTIFACT_NOT_ADVANCED")
        prior_final = round_2
    if len(update_artifact_ids) != 2 * len(ordered) or len(set(update_artifact_ids)) != len(
        update_artifact_ids
    ):
        findings.append("ONLINE_ARTIFACT_CHAIN_NOT_UNIQUE")
    return findings


def _require_nonnegative_int(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")


__all__ = [
    "CANARY9_LABELS",
    "PROTOCOL_ID",
    "PROTOCOL_LABELS",
    "PrivateTaskwiseRoundResultV1",
    "TaskwiseCanaryEvidenceV1",
    "build_taskwise_canary9_report_v1",
    "build_taskwise_online_report_v1",
]
