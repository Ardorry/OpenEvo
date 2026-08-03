from __future__ import annotations

from openevo_chembench.temperature_safe_evolve_v2.gates import (
    InfrastructureOutcomeV2,
    PairedGateInputV2,
    PhaseAArmGateV2,
    decide_deployment_v2,
    decide_promotion_v2,
    r0_continue_v2,
    select_phase_a_target_v2,
)


def _infra(count: int) -> InfrastructureOutcomeV2:
    return InfrastructureOutcomeV2(count, count, 0, 0, 0)


def _pair(reference: tuple[bool, ...], candidate: tuple[bool, ...]) -> PairedGateInputV2:
    return PairedGateInputV2(reference, candidate, _infra(len(reference)), _infra(len(candidate)))


def test_phase_a_gate_and_fixed_tie_break_select_skill() -> None:
    base = (False, True, True, True)
    better = (True, True, True, True)
    half_base = (False, True)
    half_better = (True, True)
    gate1 = PhaseAArmGateV2(
        "A1",
        _pair(base, better),
        _pair(half_base, half_better),
        _pair((True, True), (True, True)),
        True,
        3455,
    )
    gate2 = PhaseAArmGateV2(
        "A2",
        _pair(base, better),
        _pair(half_base, half_better),
        _pair((True, True), (True, True)),
        True,
        784,
    )
    selected, receipt = select_phase_a_target_v2(gate1, gate2)
    assert selected == "skill_bundle"
    assert receipt["status"] == "SELECTED"


def test_promotion_deployment_and_r0_stop_are_fail_closed() -> None:
    reference = (True, True, False, False)
    harmful = (False, True, True, False)
    candidate = _pair(reference, harmful)
    promotion = decide_promotion_v2(
        incremental=candidate,
        versus_g0=candidate,
        artifact_audit_passed=True,
    )
    assert not promotion.promoted
    assert "incremental_utility_not_positive" in promotion.reasons

    deployment = decide_deployment_v2(
        versus_g0=candidate,
        cumulative_promoted_forward_utility=1,
        promotion_count=1,
        audit_passed=True,
    )
    assert not deployment.deployed_real_evolved
    continued, reasons = r0_continue_v2(
        deployment=deployment,
        raw_test=candidate,
        promotion_count=1,
        integrity_passed=True,
    )
    assert not continued
    assert "v2_deployment_gate_failed" in reasons
