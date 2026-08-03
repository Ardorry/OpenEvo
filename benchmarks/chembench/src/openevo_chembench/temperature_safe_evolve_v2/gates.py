"""Preregistered Phase A, promotion, deployment, retirement and R0 gates."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from openevo_chembench.temperature_full_evolve_v1.statistics import (
    PairedBinaryMetricsV1,
    paired_binary_metrics_v1,
)
from openevo_chembench.temperature_safe_evolve_v2.artifacts import CanonicalEntryV2


@dataclass(frozen=True, slots=True)
class InfrastructureOutcomeV2:
    accepted_count: int
    parser_success_count: int
    retry_count: int
    failure_count: int
    timeout_count: int

    def __post_init__(self) -> None:
        values = (
            self.accepted_count,
            self.parser_success_count,
            self.retry_count,
            self.failure_count,
            self.timeout_count,
        )
        if any(type(value) is not int or value < 0 for value in values):
            raise ValueError("infrastructure outcome is invalid")
        if self.parser_success_count > self.accepted_count:
            raise ValueError("parser count exceeds accepted count")


@dataclass(frozen=True, slots=True)
class PairedGateInputV2:
    reference: tuple[bool, ...]
    candidate: tuple[bool, ...]
    reference_infrastructure: InfrastructureOutcomeV2
    candidate_infrastructure: InfrastructureOutcomeV2

    def __post_init__(self) -> None:
        if (
            not self.reference
            or len(self.reference) != len(self.candidate)
            or any(type(value) is not bool for value in (*self.reference, *self.candidate))
            or self.reference_infrastructure.accepted_count != len(self.reference)
            or self.candidate_infrastructure.accepted_count != len(self.candidate)
        ):
            raise ValueError("paired gate input is invalid")

    @property
    def infrastructure_equal(self) -> bool:
        left = self.reference_infrastructure
        right = self.candidate_infrastructure
        return (
            left.parser_success_count,
            left.retry_count,
            left.failure_count,
            left.timeout_count,
        ) == (
            right.parser_success_count,
            right.retry_count,
            right.failure_count,
            right.timeout_count,
        )

    @property
    def metrics(self) -> PairedBinaryMetricsV1:
        return _paired_metrics(self.reference, self.candidate)

    @property
    def positive_flips(self) -> int:
        return self.metrics.candidate_only_correct

    @property
    def negative_flips(self) -> int:
        return self.metrics.reference_only_correct

    @property
    def utility(self) -> int:
        return self.positive_flips - 2 * self.negative_flips


@lru_cache(maxsize=256)
def _paired_metrics(
    reference: tuple[bool, ...], candidate: tuple[bool, ...]
) -> PairedBinaryMetricsV1:
    return paired_binary_metrics_v1(
        reference,
        candidate,
        bootstrap_seed=20260803,
    )


@dataclass(frozen=True, slots=True)
class PhaseAArmGateV2:
    arm_id: Literal["A1", "A2"]
    overall: PairedGateInputV2
    h0: PairedGateInputV2
    h1: PairedGateInputV2
    artifact_hash_valid: bool
    injected_bytes: int

    @property
    def qualified(self) -> bool:
        return (
            self.overall.utility > 0
            and self.overall.positive_flips > self.overall.negative_flips
            and self.overall.negative_flips <= 2
            and self.h0.metrics.delta_percentage_points >= 0
            and self.h1.metrics.delta_percentage_points >= 0
            and self.h0.utility >= 0
            and self.h1.utility >= 0
            and self.overall.infrastructure_equal
            and self.h0.infrastructure_equal
            and self.h1.infrastructure_equal
            and self.artifact_hash_valid
        )

    def receipt(self) -> dict[str, object]:
        return {
            "arm_id": self.arm_id,
            "qualified": self.qualified,
            "utility": self.overall.utility,
            "positive_flips": self.overall.positive_flips,
            "negative_flips": self.overall.negative_flips,
            "accuracy": self.overall.metrics.candidate_accuracy,
            "h0_delta_pp": self.h0.metrics.delta_percentage_points,
            "h1_delta_pp": self.h1.metrics.delta_percentage_points,
            "h0_utility": self.h0.utility,
            "h1_utility": self.h1.utility,
            "infrastructure_parity": self.overall.infrastructure_equal,
            "artifact_hash_valid": self.artifact_hash_valid,
            "injected_bytes": self.injected_bytes,
        }


def select_phase_a_target_v2(
    memory: PhaseAArmGateV2,
    skill: PhaseAArmGateV2,
) -> tuple[Literal["text_memory", "skill_bundle"] | None, dict[str, object]]:
    if memory.arm_id != "A1" or skill.arm_id != "A2":
        raise ValueError("Phase A single-target arms are invalid")
    eligible = [value for value in (memory, skill) if value.qualified]
    selected: Literal["text_memory", "skill_bundle"] | None
    if not eligible:
        selected = None
    elif len(eligible) == 1:
        selected = "text_memory" if eligible[0].arm_id == "A1" else "skill_bundle"
    else:
        ranked = sorted(
            eligible,
            key=lambda value: (
                -value.overall.utility,
                value.overall.negative_flips,
                -value.overall.metrics.candidate_accuracy,
                value.injected_bytes,
                0 if value.arm_id == "A2" else 1,
            ),
        )
        selected = "text_memory" if ranked[0].arm_id == "A1" else "skill_bundle"
    return selected, {
        "schema_version": "TemperatureSafePhaseATargetSelectionReceiptV2",
        "status": "NO_GO_ABLATION_NO_SAFE_SINGLE_TARGET" if selected is None else "SELECTED",
        "selected_target": selected,
        "memory_only": memory.receipt(),
        "skill_only": skill.receipt(),
        "tie_break_order": [
            "utility_desc",
            "negative_flips_asc",
            "accuracy_desc",
            "injected_bytes_asc",
            "skill_bundle",
        ],
    }


@dataclass(frozen=True, slots=True)
class PromotionDecisionV2:
    promoted: bool
    incremental: PairedGateInputV2
    versus_g0: PairedGateInputV2
    artifact_audit_passed: bool
    reasons: tuple[str, ...]


def decide_promotion_v2(
    *,
    incremental: PairedGateInputV2,
    versus_g0: PairedGateInputV2,
    artifact_audit_passed: bool,
) -> PromotionDecisionV2:
    checks = {
        "incremental_utility_not_positive": incremental.utility > 0,
        "incremental_positive_not_gt_negative": incremental.positive_flips
        > incremental.negative_flips,
        "incremental_negative_exceeded": incremental.negative_flips <= 1,
        "candidate_accuracy_below_active": incremental.metrics.candidate_accuracy
        >= incremental.metrics.reference_accuracy,
        "baseline_utility_negative": versus_g0.utility >= 0,
        "candidate_accuracy_below_g0": versus_g0.metrics.candidate_accuracy
        >= versus_g0.metrics.reference_accuracy,
        "infrastructure_mismatch": incremental.infrastructure_equal
        and versus_g0.infrastructure_equal,
        "artifact_audit_failed": artifact_audit_passed,
    }
    reasons = tuple(key for key, passed in checks.items() if not passed)
    return PromotionDecisionV2(
        promoted=not reasons,
        incremental=incremental,
        versus_g0=versus_g0,
        artifact_audit_passed=artifact_audit_passed,
        reasons=reasons,
    )


@dataclass(frozen=True, slots=True)
class DeploymentDecisionV2:
    deployed_real_evolved: bool
    versus_g0: PairedGateInputV2
    cumulative_promoted_forward_utility: int
    promotion_count: int
    audit_passed: bool
    reasons: tuple[str, ...]


def decide_deployment_v2(
    *,
    versus_g0: PairedGateInputV2,
    cumulative_promoted_forward_utility: int,
    promotion_count: int,
    audit_passed: bool,
) -> DeploymentDecisionV2:
    checks = {
        "v2_utility_not_positive": versus_g0.utility > 0,
        "v2_positive_not_gt_negative": versus_g0.positive_flips > versus_g0.negative_flips,
        "v2_negative_exceeded": versus_g0.negative_flips <= 1,
        "v2_accuracy_below_g0": versus_g0.metrics.candidate_accuracy
        >= versus_g0.metrics.reference_accuracy,
        "cumulative_promoted_forward_utility_not_positive": cumulative_promoted_forward_utility
        > 0,
        "no_candidate_promotion": promotion_count >= 1,
        "infrastructure_mismatch": versus_g0.infrastructure_equal,
        "deployment_audit_failed": audit_passed,
    }
    reasons = tuple(key for key, passed in checks.items() if not passed)
    return DeploymentDecisionV2(
        deployed_real_evolved=not reasons,
        versus_g0=versus_g0,
        cumulative_promoted_forward_utility=cumulative_promoted_forward_utility,
        promotion_count=promotion_count,
        audit_passed=audit_passed,
        reasons=reasons,
    )


def retirement_reasons_v2(
    entry: CanonicalEntryV2,
    *,
    evaluated_block_utilities: tuple[int, ...],
    blocks_without_new_support: int,
    stronger_unconditional_conflict: bool,
    safety_failure: bool,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if entry.negative_flip_count >= 2 and entry.positive_flip_count <= entry.negative_flip_count:
        reasons.append("negative_flip_utility")
    if entry.support_count and entry.contradiction_count / entry.support_count > 0.5:
        reasons.append("contradiction_support_ratio")
    if len(evaluated_block_utilities) >= 2 and all(
        value <= 0 for value in evaluated_block_utilities[-2:]
    ):
        reasons.append("two_nonpositive_blocks")
    if blocks_without_new_support >= 2 and entry.contradiction_count > 0:
        reasons.append("stale_with_contradiction")
    if stronger_unconditional_conflict:
        reasons.append("stronger_unconditional_conflict")
    if safety_failure:
        reasons.append("schema_length_or_retrieval_safety")
    return tuple(reasons)


def r0_continue_v2(
    *,
    deployment: DeploymentDecisionV2,
    raw_test: PairedGateInputV2,
    promotion_count: int,
    integrity_passed: bool,
) -> tuple[bool, tuple[str, ...]]:
    checks = {
        "v2_deployment_gate_failed": deployment.deployed_real_evolved,
        "no_promotion": promotion_count >= 1,
        "raw_test_accuracy_below_g0": raw_test.metrics.candidate_accuracy
        >= raw_test.metrics.reference_accuracy,
        "raw_test_positive_below_negative": raw_test.positive_flips >= raw_test.negative_flips,
        "raw_test_negative_exceeded": raw_test.negative_flips <= 2,
        "raw_test_utility_negative": raw_test.utility >= 0,
        "test_infrastructure_mismatch": raw_test.infrastructure_equal,
        "integrity_failed": integrity_passed,
    }
    reasons = tuple(key for key, passed in checks.items() if not passed)
    return not reasons, reasons


__all__ = [
    "DeploymentDecisionV2",
    "InfrastructureOutcomeV2",
    "PairedGateInputV2",
    "PhaseAArmGateV2",
    "PromotionDecisionV2",
    "decide_deployment_v2",
    "decide_promotion_v2",
    "r0_continue_v2",
    "retirement_reasons_v2",
    "select_phase_a_target_v2",
]
