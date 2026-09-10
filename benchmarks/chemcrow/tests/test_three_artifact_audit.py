from __future__ import annotations

import json
from copy import deepcopy

import pytest

from openevo_chemcrow.hashing import canonical_sha256
from openevo_chemcrow.paper_evaluator import (
    FROZEN_PAPER_TASK_IDS,
    assert_sealed_run_ready,
)
from openevo_chemcrow.runtime import CORE_MANAGED_CODEX_ROUTE
from openevo_chemcrow.three_artifact_audit import (
    _audit_reflector_completion,
    _audit_verified_no_effect_attempts,
    _record_globally_unique_three,
)
from openevo_chemcrow.three_artifact_models import (
    CORE_NATIVE_THREE_ARTIFACT_PROTOCOL_LABEL,
)


def _valid_evolved_claim() -> tuple[dict, dict]:
    pair_id = "pair"
    task_id = "task"
    replacement_run_id = "pair-evolved-2222222222"
    artifact_ids = ["memory", "skill", "agent"]
    authority = {
        "task_id": task_id,
        "artifact_count": 3,
        "artifact_ids_by_type": {
            "text_memory": artifact_ids[0],
            "skill_bundle": artifact_ids[1],
            "agent_system": artifact_ids[2],
        },
    }
    authority_sha256 = canonical_sha256(authority)
    receipt = {
        "schema_version": "chemcrow_evolved_candidate_no_effect_recovery_v1",
        "status": "VERIFIED_NO_EVOLVED_CANDIDATE_EFFECT_REPLACEMENT_READY",
        "pair_id": pair_id,
        "task_id": task_id,
        "task_authority_sha256": "1" * 64,
        "phase": "evolved_candidate",
        "attempt_ordinal": 1,
        "authority_sha256": authority_sha256,
        "original_claim_sha256": "a" * 64,
        "config_sha256": "2" * 64,
        "checkpoint_sha256": "3" * 64,
        "active_attempt_run_id": "pair-evolved-1111111111",
        "replacement_run_id": replacement_run_id,
        "core_task_id": "pair-evolved-1111111111",
        "core_session_id_sha256": "4" * 64,
        "core_completion_sha256": "5" * 64,
        "core_completion_status": "ERROR",
        "error_category": "credential_isolation_validation_failed",
        "input_evidence_sha256": "6" * 64,
        "artifact_ids_by_type": authority["artifact_ids_by_type"],
        "reflector_job_ids": ["job-1", "job-2", "job-3"],
        "reflector_run_ids": ["run-1", "run-2", "run-3"],
        "reflector_prompt_hashes": ["7" * 64, "8" * 64, "9" * 64],
        "injection_authority": {
            "evolved_claim_authority_sha256": authority_sha256,
            "context_id": "context",
            "context_artifact_ids": artifact_ids,
            "context_injected": True,
            "revision_id": "chemcrow-task-local-three:" + canonical_sha256(artifact_ids),
            "runtime_injection_receipt_published": False,
            "credential_readiness_receipt_published": False,
        },
        "no_effect_predicates": {
            "core_status_error": True,
            "exact_setup_canary_validation_error": True,
            "trajectory_has_zero_records": True,
            "trajectory_has_zero_traces": True,
            "workspace_result_absent": True,
            "core_route_bound": True,
            "host_codex_exec_forbidden": True,
            "context_injected_before_setup": True,
            "exact_three_context_artifact_ids": True,
            "context_identity_present": True,
            "runtime_injection_receipt_not_published": True,
            "credential_contract_present": True,
            "credential_readiness_receipt_not_published": True,
            "revision_authority_exact": True,
            "session_identity_present": True,
        },
        "infrastructure_canary_model_call_may_have_occurred": True,
        "evolved_candidate_model_call_proven_absent": True,
        "prior_scientific_calls_preserved": [
            "baseline_candidate",
            "baseline_internal_evaluator",
            "reflector_memory",
            "reflector_skill_bundle",
            "reflector_agent_system",
        ],
        "prior_scientific_calls_redispatched": False,
        "replacement_scope": ["evolved_candidate"],
        "duplicate_scientific_call": False,
        "recorded_before_replacement_dispatch": True,
    }
    terminal_receipt = {"run_id": replacement_run_id}
    claim = {
        "schema_version": "chemcrow_phase_claim_v2",
        "phase": "evolved_candidate",
        "status": "terminal",
        "authority": authority,
        "authority_sha256": authority_sha256,
        "failed_attempts": [
            {
                "attempt_ordinal": 1,
                "outcome": "terminal_evolved_candidate_no_effect",
                "receipt": receipt,
                "receipt_sha256": canonical_sha256(receipt),
            }
        ],
        "active_attempt_ordinal": 2,
        "active_attempt_run_id": replacement_run_id,
        "replacement_activated": True,
        "replacement_activation_token_sha256": "b" * 64,
        "receipt": terminal_receipt,
        "receipt_sha256": canonical_sha256(terminal_receipt),
    }
    expected = {
        "task_id": task_id,
        "replacement_run_id": replacement_run_id,
        "input_evidence_sha256": receipt["input_evidence_sha256"],
        "artifact_ids_by_type": receipt["artifact_ids_by_type"],
        "reflector_job_ids": receipt["reflector_job_ids"],
        "reflector_run_ids": receipt["reflector_run_ids"],
        "reflector_prompt_hashes": receipt["reflector_prompt_hashes"],
    }
    return claim, expected


def _audit_evolved_claim(claim: dict, expected: dict, *, phase: str = "evolved_candidate"):
    return _audit_verified_no_effect_attempts(
        claim=claim,
        pair_id="pair",
        phase=phase,
        expected_task_id="task",
        expected_evolved_authority=expected,
    )


def test_audit_accepts_exact_evolved_candidate_no_effect_authority():
    claim, expected = _valid_evolved_claim()

    assert _audit_evolved_claim(claim, expected) == (0, 0, 0, 1)


def test_audit_counts_verified_baseline_evaluator_no_effect_attempt():
    authority = {
        "task_id": "task",
        "run_id": "baseline-run",
        "evaluator_id": "evaluator",
        "paper_evaluator": False,
    }
    receipt = {
        "schema_version": "chemcrow_baseline_evaluator_no_effect_recovery_v1",
        "status": "VERIFIED_NO_CANDIDATE_EFFECT_REPLACEMENT_READY",
        "pair_id": "pair",
        "phase": "baseline_internal_evaluator",
        "attempt_ordinal": 1,
        "authority_sha256": canonical_sha256(authority),
        "evaluator_model_call_proven_absent": True,
        "duplicate_scientific_call": False,
        "recorded_before_replacement_dispatch": True,
        "no_effect_predicates": {"exact_setup_failure": True},
    }
    terminal_receipt = {"evaluator_run_id": "replacement-evaluator"}
    claim = {
        "schema_version": "chemcrow_phase_claim_v2",
        "phase": "baseline_internal_evaluator",
        "status": "terminal",
        "authority": authority,
        "authority_sha256": canonical_sha256(authority),
        "failed_attempts": [
            {
                "attempt_ordinal": 1,
                "outcome": "terminal_baseline_evaluator_no_effect",
                "receipt": receipt,
                "receipt_sha256": canonical_sha256(receipt),
            }
        ],
        "active_attempt_ordinal": 2,
        "replacement_activated": True,
        "replacement_activation_token_sha256": "a" * 64,
        "receipt": terminal_receipt,
        "receipt_sha256": canonical_sha256(terminal_receipt),
    }

    assert _audit_verified_no_effect_attempts(
        claim=claim,
        pair_id="pair",
        phase="baseline_internal_evaluator",
        expected_task_id="task",
        expected_evolved_authority={},
    ) == (0, 1, 0, 0)


def test_audit_rejects_evolved_candidate_receipt_on_wrong_phase():
    claim, expected = _valid_evolved_claim()

    with pytest.raises(ValueError, match="phase claim authority differs"):
        _audit_evolved_claim(claim, expected, phase="reflector_memory")


def test_audit_rejects_evolved_candidate_receipt_on_wrong_schema():
    claim, expected = _valid_evolved_claim()
    attempt = claim["failed_attempts"][0]
    attempt["receipt"]["schema_version"] = "chemcrow_reflector_no_effect_recovery_v1"
    attempt["receipt_sha256"] = canonical_sha256(attempt["receipt"])

    with pytest.raises(ValueError, match="failed-attempt receipt is invalid"):
        _audit_evolved_claim(claim, expected)


def test_audit_rejects_non_exact_fifteen_predicates():
    claim, expected = _valid_evolved_claim()
    attempt = claim["failed_attempts"][0]
    attempt["receipt"]["no_effect_predicates"]["session_identity_present"] = False
    attempt["receipt_sha256"] = canonical_sha256(attempt["receipt"])

    with pytest.raises(ValueError):
        _audit_evolved_claim(claim, expected)


def test_audit_rejects_evolved_receipt_not_bound_to_sealed_reflector_authority():
    claim, expected = _valid_evolved_claim()
    wrong = deepcopy(expected)
    wrong["reflector_job_ids"] = ["other-1", "other-2", "other-3"]

    with pytest.raises(ValueError, match="scientific authority differs"):
        _audit_evolved_claim(claim, wrong)


@pytest.mark.parametrize(
    "authority_name",
    ["artifact ID", "Reflector job ID", "Reflector run ID"],
)
def test_audit_rejects_cross_task_identity_reuse(authority_name):
    seen: set[str] = set()
    _record_globally_unique_three(
        seen,
        ["one", "two", "three"],
        authority_name=authority_name,
        pair_id="pair-1",
    )
    _record_globally_unique_three(
        seen,
        ["four", "five", "six"],
        authority_name=authority_name,
        pair_id="pair-2",
    )
    assert len(seen) == 6

    with pytest.raises(ValueError, match="cross-task"):
        _record_globally_unique_three(
            seen,
            ["seven", "one", "eight"],
            authority_name=authority_name,
            pair_id="pair-3",
        )


def test_generation_audit_accepts_core_managed_reflector_completion(tmp_path):
    run_id = "reflector-run"
    completion_root = tmp_path / "completions"
    completion_dir = completion_root / f"task_{run_id}"
    completion_dir.mkdir(parents=True)
    payload = {
        "status": "COMPLETED",
        "task_id": run_id,
        "trajectory": {
            "metadata": {
                "task_metadata": {
                    "execution_route": CORE_MANAGED_CODEX_ROUTE,
                    "host_codex_exec_forbidden": True,
                    "agent_model_name": "gpt-5.5",
                    "openevo": {"credential_isolation_receipt": {"status": "passed"}},
                }
            }
        },
    }
    (completion_dir / "completion.json").write_text(json.dumps(payload), encoding="utf-8")

    _audit_reflector_completion(
        core_completion_root=completion_root,
        run_id=run_id,
        expected_model="gpt-5.5",
    )


def test_generation_audit_rejects_host_reflector_completion(tmp_path):
    run_id = "reflector-run"
    completion_root = tmp_path / "completions"
    completion_dir = completion_root / f"task_{run_id}"
    completion_dir.mkdir(parents=True)
    payload = {
        "status": "COMPLETED",
        "task_id": run_id,
        "trajectory": {
            "metadata": {
                "task_metadata": {
                    "execution_route": "host_codex_exec",
                    "host_codex_exec_forbidden": False,
                    "agent_model_name": "gpt-5.5",
                    "openevo": {"credential_isolation_receipt": {"status": "passed"}},
                }
            }
        },
    }
    (completion_dir / "completion.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="Reflector Core completion provenance differs"):
        _audit_reflector_completion(
            core_completion_root=completion_root,
            run_id=run_id,
            expected_model="gpt-5.5",
        )


@pytest.mark.parametrize(
    "missing_field",
    [
        "unique_reflector_job_count",
        "unique_reflector_run_count",
        "independent_reflector_run_count",
    ],
)
def test_paper_preflight_requires_global_reflector_identity_counts(tmp_path, missing_field):
    run_root = tmp_path / "run"
    run_root.mkdir()
    audit_path = tmp_path / "audit.json"
    audit = {
        "status": "PASS",
        "experiment_id": "experiment",
        "task_ids": list(FROZEN_PAPER_TASK_IDS),
        "task_count": len(FROZEN_PAPER_TASK_IDS),
        "artifact_protocol": CORE_NATIVE_THREE_ARTIFACT_PROTOCOL_LABEL,
        "unique_artifact_count": 42,
        "unique_reflector_job_count": 42,
        "unique_reflector_run_count": 42,
        "independent_reflector_job_count": 42,
        "independent_reflector_run_count": 42,
        "core_evolved_injection_receipt_count": 14,
        "mock_or_fixture_observations": 0,
    }
    audit.pop(missing_field)
    audit_path.write_text(json.dumps(audit), encoding="utf-8")

    with pytest.raises(ValueError, match="completed-run audit"):
        assert_sealed_run_ready(
            run_root=run_root,
            experiment_id="experiment",
            task_ids=list(FROZEN_PAPER_TASK_IDS),
            completed_run_audit=audit_path,
        )
