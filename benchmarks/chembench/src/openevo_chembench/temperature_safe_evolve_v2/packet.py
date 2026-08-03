"""Train-only supervised packet construction and Test-isolation sealing."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace

from openevo_chembench.chembench4k_dataset import normalize_benchmark_text
from openevo_chembench.chembench4k_models import PrivateChemBench4KTask
from openevo_chembench.supervised_transfer_v1.common import canonical_json_bytes, sha256_bytes
from openevo_chembench.temperature_safe_evolve_v2.artifacts import (
    SafeArtifactStateV2,
    SelectedTarget,
)
from openevo_chembench.temperature_safe_evolve_v2.candidate import SafeEvaluatedCandidateV2

_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)


class SafePacketError(RuntimeError):
    def __init__(self, finding_code: str) -> None:
        self.finding_code = finding_code
        super().__init__(finding_code)


@dataclass(frozen=True, slots=True, repr=False)
class ReflectorArmRecordV2:
    logical_label: str
    response: str
    response_sha256: str
    parsed_prediction: str | None
    parser_status: str
    correct: bool
    context_hash: str
    selected_entry_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not self.logical_label
            or hashlib.sha256(self.response.encode()).hexdigest() != self.response_sha256
            or self.parsed_prediction not in {None, "A", "B", "C", "D"}
            or type(self.correct) is not bool
            or _SHA256.fullmatch(self.context_hash) is None
            or tuple(sorted(self.selected_entry_ids)) != self.selected_entry_ids
        ):
            raise ValueError("Reflector arm record is invalid")

    def to_private_dict(self) -> dict[str, object]:
        return {
            "logical_label": self.logical_label,
            "candidate_raw_output": self.response,
            "candidate_response_sha256": self.response_sha256,
            "parsed_prediction": self.parsed_prediction,
            "parser_status": self.parser_status,
            "correct": self.correct,
            "context_hash": self.context_hash,
            "selected_entry_ids": list(self.selected_entry_ids),
        }

    def __repr__(self) -> str:
        return "ReflectorArmRecordV2(<completion-redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class ReflectorTrainItemV2:
    task: PrivateChemBench4KTask
    arms: tuple[ReflectorArmRecordV2, ...]

    def __post_init__(self) -> None:
        if (
            type(self.task) is not PrivateChemBench4KTask
            or not self.arms
            or len({value.logical_label for value in self.arms}) != len(self.arms)
        ):
            raise ValueError("Reflector Train item is invalid")

    def to_private_dict(self) -> dict[str, object]:
        return {
            "uid": self.task.uid,
            "question": self.task.question,
            "options": {
                "A": self.task.A,
                "B": self.task.B,
                "C": self.task.C,
                "D": self.task.D,
            },
            "ground_truth": self.task.target,
            "correct_option": getattr(self.task, self.task.target),
            "arms": [value.to_private_dict() for value in self.arms],
        }

    def __repr__(self) -> str:
        return "ReflectorTrainItemV2(<task-GT-and-completions-redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class SealedReflectorPacketV2:
    run_id: str
    fold_id: str
    batch_index: int
    selected_target: SelectedTarget
    active_state: SafeArtifactStateV2
    items: tuple[ReflectorTrainItemV2, ...]
    cumulative_evidence: dict[str, object]
    promotion_history: tuple[dict[str, object], ...]
    aggregate_forward_history: tuple[dict[str, object], ...]
    required_response_bindings: dict[str, object]
    packet_sha256: str
    prior_evidence_sha256: str
    test_isolation_receipt_sha256: str
    seen_train_uids: frozenset[str]
    current_batch_uids: frozenset[str]
    forbidden_normalized_questions: tuple[str, ...]

    def __repr__(self) -> str:
        return "SealedReflectorPacketV2(<Train-GT-evidence-redacted>)"

    def visible_payload(self) -> dict[str, object]:
        return {
            "schema_version": "TemperatureSafeReflectorVisiblePacketV2",
            "run_id": self.run_id,
            "fold_id": self.fold_id,
            "batch_index": self.batch_index,
            "selected_target": self.selected_target,
            "current_active_artifact": self.active_state.model_dump(mode="json"),
            "current_train_batch": [item.to_private_dict() for item in self.items],
            "cumulative_structured_evidence": self.cumulative_evidence,
            "promotion_and_rejection_history": list(self.promotion_history),
            "aggregate_forward_history": list(self.aggregate_forward_history),
            "optimization_objective": {
                "repair_residual_errors": True,
                "negative_flip_penalty": 2,
                "preserve_g0_correct_judgments": True,
                "avoid_overbroad_rules": True,
                "preserve_applicability_and_counterexamples": True,
                "actively_retire_harmful_entries": True,
                "active_entry_limit": 6,
                "sparse_injection_required": True,
                "memorize_answers": False,
            },
            "required_response_bindings": self.required_response_bindings,
        }


def build_reflector_packet_v2(
    *,
    run_id: str,
    fold_id: str,
    batch_index: int,
    selected_target: SelectedTarget,
    active_state: SafeArtifactStateV2,
    items: tuple[ReflectorTrainItemV2, ...],
    cumulative_evidence: dict[str, object],
    promotion_history: tuple[dict[str, object], ...],
    aggregate_forward_history: tuple[dict[str, object], ...],
    seen_train_uids: frozenset[str],
    seen_train_tasks: tuple[PrivateChemBench4KTask, ...],
    test_tasks: tuple[PrivateChemBench4KTask, ...],
) -> SealedReflectorPacketV2:
    """Seal one packet after proving it contains only the current run's Train data."""

    if (
        type(active_state) is not SafeArtifactStateV2
        or not 1 <= batch_index <= 4
        or len(items) != 25
        or any(type(value) is not ReflectorTrainItemV2 for value in items)
        or type(cumulative_evidence) is not dict
        or type(seen_train_tasks) is not tuple
        or type(test_tasks) is not tuple
        or not test_tasks
    ):
        raise TypeError("Reflector packet inputs are invalid")
    current = frozenset(item.task.uid for item in items)
    visible_train_uids = frozenset(task.uid for task in seen_train_tasks)
    test_uids = frozenset(task.uid for task in test_tasks)
    if (
        len(current) != 25
        or not current.issubset(seen_train_uids)
        or visible_train_uids != seen_train_uids
        or any(type(task) is not PrivateChemBench4KTask for task in seen_train_tasks)
        or seen_train_uids & test_uids
        or active_state.run_id != run_id
        or active_state.fold_id != fold_id
        or active_state.selected_target != selected_target
    ):
        raise SafePacketError("SAFE_REFLECTOR_TRAIN_TEST_SCOPE_INVALID")
    prior_evidence_sha = sha256_bytes(canonical_json_bytes(cumulative_evidence))
    source_body = {
        "run_id": run_id,
        "fold_id": fold_id,
        "batch_index": batch_index,
        "selected_target": selected_target,
        "predecessor_artifact_sha256": active_state.projection_sha256,
        "prior_evidence_sha256": prior_evidence_sha,
        "current_active_artifact": active_state.model_dump(mode="json"),
        "items": [item.to_private_dict() for item in items],
        "cumulative_structured_evidence": cumulative_evidence,
        "promotion_history": list(promotion_history),
        "aggregate_forward_history": list(aggregate_forward_history),
    }
    source_packet_sha = sha256_bytes(canonical_json_bytes(source_body))
    bindings = {
        "run_id": run_id,
        "fold_id": fold_id,
        "batch_index": batch_index,
        "selected_target": selected_target,
        "predecessor_artifact_sha256": active_state.projection_sha256,
        "prior_evidence_sha256": prior_evidence_sha,
        "source_packet_sha256": source_packet_sha,
    }
    preliminary = SealedReflectorPacketV2(
        run_id=run_id,
        fold_id=fold_id,
        batch_index=batch_index,
        selected_target=selected_target,
        active_state=active_state,
        items=items,
        cumulative_evidence=cumulative_evidence,
        promotion_history=promotion_history,
        aggregate_forward_history=aggregate_forward_history,
        required_response_bindings=bindings,
        packet_sha256="0" * 64,
        prior_evidence_sha256=prior_evidence_sha,
        test_isolation_receipt_sha256="0" * 64,
        seen_train_uids=seen_train_uids,
        current_batch_uids=current,
        forbidden_normalized_questions=tuple(
            sorted(
                " ".join(normalize_benchmark_text(task.question).casefold().split())
                for task in (*seen_train_tasks, *test_tasks)
            )
        ),
    )
    visible = preliminary.visible_payload()
    encoded = canonical_json_bytes(visible)
    if any(uid.encode("ascii") in encoded for uid in test_uids):
        raise SafePacketError("SAFE_REFLECTOR_TEST_UID_LEAKAGE")
    if _contains_test_field(visible):
        raise SafePacketError("SAFE_REFLECTOR_TEST_FIELD_LEAKAGE")
    isolation = sha256_bytes(
        canonical_json_bytes(
            {
                "schema_version": "TemperatureSafeReflectorTestIsolationReceiptV2",
                "run_id": run_id,
                "fold_id": fold_id,
                "batch_index": batch_index,
                "train_uid_count": len(seen_train_uids),
                "test_uid_count": len(test_uids),
                "uid_intersection_count": 0,
                "visible_packet_contains_test_uid": False,
                "visible_packet_contains_test_field": False,
                "test_values_disclosed": False,
            }
        )
    )
    return replace(
        preliminary,
        packet_sha256=sha256_bytes(encoded),
        test_isolation_receipt_sha256=isolation,
    )


def arm_record_from_evaluation_v2(
    value: SafeEvaluatedCandidateV2,
    *,
    logical_label: str,
    selected_entry_ids: tuple[str, ...],
) -> ReflectorArmRecordV2:
    if type(value) is not SafeEvaluatedCandidateV2:
        raise TypeError("evaluation must be exact SafeEvaluatedCandidateV2")
    return ReflectorArmRecordV2(
        logical_label=logical_label,
        response=value.accepted.observed.attempt.response,
        response_sha256=value.accepted.observed.response_sha256,
        parsed_prediction=value.evaluation.official.prediction,
        parser_status=value.evaluation.official.status.value,
        correct=value.evaluation.correct,
        context_hash=value.accepted.plan.context_hash,
        selected_entry_ids=selected_entry_ids,
    )


def _contains_test_field(value: object) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if normalized.startswith("test_") or normalized.endswith("_test"):
                return True
            if _contains_test_field(item):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_test_field(item) for item in value)
    return False


__all__ = [
    "ReflectorArmRecordV2",
    "ReflectorTrainItemV2",
    "SafePacketError",
    "SealedReflectorPacketV2",
    "arm_record_from_evaluation_v2",
    "build_reflector_packet_v2",
]
