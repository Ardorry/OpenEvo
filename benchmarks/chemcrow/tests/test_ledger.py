from __future__ import annotations

import json
import multiprocessing
from copy import deepcopy

import pytest

from openevo_chemcrow.hashing import canonical_sha256, file_sha256
from openevo_chemcrow.ledger import AmbiguousPhaseClaimError, PhaseLedger
from openevo_chemcrow.replacement_ledger import (
    VerifiedReplacementPhaseLedger,
    verified_no_effect_attempt_count,
)


def _concurrent_replacement_activation(
    root,
    start_event,
    result_queue,
    authority,
    expectation,
):
    start_event.wait()
    ledger = VerifiedReplacementPhaseLedger(root, pair_id="pair")
    try:
        token = ledger.claim(
            "evolved_candidate",
            authority,
            allow_verified_replacement=True,
            expected_replacement=expectation,
        )
    except Exception as exc:  # noqa: BLE001 - child reports exact competing outcome
        result_queue.put(("rejected", type(exc).__name__, str(exc)))
    else:
        result_queue.put(("activated", token))


def _evolved_no_effect_receipt(*, authority, claim_path):
    artifact_ids = ["memory", "skill", "agent"]
    return {
        "schema_version": "chemcrow_evolved_candidate_no_effect_recovery_v1",
        "status": "VERIFIED_NO_EVOLVED_CANDIDATE_EFFECT_REPLACEMENT_READY",
        "pair_id": "pair",
        "task_id": "task",
        "task_authority_sha256": "1" * 64,
        "phase": "evolved_candidate",
        "attempt_ordinal": 1,
        "authority_sha256": canonical_sha256(authority),
        "original_claim_sha256": file_sha256(claim_path),
        "config_sha256": "2" * 64,
        "checkpoint_sha256": "3" * 64,
        "active_attempt_run_id": "pair-evolved-1111111111",
        "replacement_run_id": "pair-evolved-2222222222",
        "core_task_id": "pair-evolved-1111111111",
        "core_session_id_sha256": "4" * 64,
        "core_completion_sha256": "5" * 64,
        "core_completion_status": "ERROR",
        "error_category": "credential_isolation_validation_failed",
        "input_evidence_sha256": "6" * 64,
        "artifact_ids_by_type": {
            "text_memory": artifact_ids[0],
            "skill_bundle": artifact_ids[1],
            "agent_system": artifact_ids[2],
        },
        "reflector_job_ids": ["job-1", "job-2", "job-3"],
        "reflector_run_ids": ["run-1", "run-2", "run-3"],
        "reflector_prompt_hashes": ["7" * 64, "8" * 64, "9" * 64],
        "injection_authority": {
            "evolved_claim_authority_sha256": canonical_sha256(authority),
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


def test_resume_refuses_ambiguous_claim(tmp_path):
    ledger = PhaseLedger(tmp_path, pair_id="pair")
    ledger.claim("baseline_candidate", {"task": "x"})
    with pytest.raises(AmbiguousPhaseClaimError, match="do not duplicate"):
        ledger.audit_resume()


def test_terminal_claim_is_auditable(tmp_path):
    ledger = PhaseLedger(tmp_path, pair_id="pair")
    ledger.claim("baseline_candidate", {"task": "x"})
    ledger.terminal("baseline_candidate", {"run_id": "run-1"})
    assert ledger.audit_resume() == {"baseline_candidate": "terminal"}


def test_verified_no_effect_replacement_is_explicit_and_preserved(tmp_path):
    ledger = VerifiedReplacementPhaseLedger(tmp_path, pair_id="pair")
    authority = {"task_id": "task", "artifact_ids": [], "artifact_inventory": {}}
    ledger.claim("baseline_candidate", authority)
    claim_path = tmp_path / "pair" / "baseline_candidate.json"
    receipt = {
        "schema_version": "chemcrow_pre_candidate_no_effect_recovery_v1",
        "status": "VERIFIED_NO_CANDIDATE_EFFECT_REPLACEMENT_READY",
        "pair_id": "pair",
        "phase": "baseline_candidate",
        "attempt_ordinal": 1,
        "authority_sha256": canonical_sha256(authority),
        "original_claim_sha256": file_sha256(claim_path),
        "no_effect_predicates": {"zero_trajectory": True},
        "candidate_model_call_proven_absent": True,
        "duplicate_scientific_call": False,
        "recorded_before_replacement_dispatch": True,
    }
    ledger.reconcile_verified_no_effect_failure("baseline_candidate", receipt)
    assert ledger.replacement_ready("baseline_candidate") is True
    with pytest.raises(AmbiguousPhaseClaimError, match="explicit resume"):
        ledger.claim("baseline_candidate", authority)
    with pytest.raises(AmbiguousPhaseClaimError, match="do not duplicate"):
        ledger.audit_resume()

    expectation = ledger.replacement_expectation("baseline_candidate")
    activation = ledger.claim(
        "baseline_candidate",
        authority,
        allow_verified_replacement=True,
        expected_replacement=expectation,
    )
    with pytest.raises(AmbiguousPhaseClaimError, match="do not duplicate"):
        ledger.audit_resume()
    ledger.terminal(
        "baseline_candidate",
        {"run_id": "replacement-run"},
        replacement_activation=activation,
    )
    payload = __import__("json").loads(claim_path.read_text())
    assert payload["active_attempt_ordinal"] == 2
    assert payload["replacement_activated"] is True
    assert (
        verified_no_effect_attempt_count(
            payload,
            pair_id="pair",
            phase="baseline_candidate",
        )
        == 1
    )
    assert ledger.audit_resume() == {"baseline_candidate": "terminal"}


def test_verified_reflector_no_effect_replacement_is_explicit_and_preserved(tmp_path):
    ledger = VerifiedReplacementPhaseLedger(tmp_path, pair_id="pair")
    phase = "reflector_memory"
    authority = {"task_id": "task", "artifact_type": "text_memory"}
    ledger.claim(phase, authority)
    claim_path = tmp_path / "pair" / f"{phase}.json"
    receipt = {
        "schema_version": "chemcrow_reflector_no_effect_recovery_v1",
        "status": "VERIFIED_NO_CANDIDATE_EFFECT_REPLACEMENT_READY",
        "pair_id": "pair",
        "phase": phase,
        "attempt_ordinal": 1,
        "authority_sha256": canonical_sha256(authority),
        "original_claim_sha256": file_sha256(claim_path),
        "no_effect_predicates": {"zero_trajectory": True},
        "reflector_model_call_proven_absent": True,
        "duplicate_scientific_call": False,
        "recorded_before_replacement_dispatch": True,
    }

    ledger.reconcile_verified_no_effect_failure(phase, receipt)
    first_expectation = ledger.replacement_expectation(phase)
    ledger.claim(
        phase,
        authority,
        allow_verified_replacement=True,
        expected_replacement=first_expectation,
    )
    second_receipt = {
        **receipt,
        "attempt_ordinal": 2,
        "original_claim_sha256": file_sha256(claim_path),
        "no_effect_predicates": {"inner_argv_limit": True},
    }
    ledger.reconcile_verified_no_effect_failure(phase, second_receipt)
    second_expectation = ledger.replacement_expectation(phase)
    activation = ledger.claim(
        phase,
        authority,
        allow_verified_replacement=True,
        expected_replacement=second_expectation,
    )
    ledger.terminal(
        phase,
        {"reflector_job_id": "job-replacement"},
        replacement_activation=activation,
    )

    payload = __import__("json").loads(claim_path.read_text())
    assert payload["failed_attempts"][0]["outcome"] == "terminal_reflector_no_effect"
    assert payload["failed_attempts"][1]["outcome"] == "terminal_reflector_no_effect"
    assert verified_no_effect_attempt_count(payload, pair_id="pair", phase=phase) == 2


def test_verified_evolved_candidate_no_effect_replacement_is_scoped_and_preserved(
    tmp_path,
):
    ledger = VerifiedReplacementPhaseLedger(tmp_path, pair_id="pair")
    phase = "evolved_candidate"
    authority = {
        "task_id": "task",
        "artifact_count": 3,
        "artifact_ids_by_type": {
            "text_memory": "memory",
            "skill_bundle": "skill",
            "agent_system": "agent",
        },
    }
    ledger.claim(phase, authority)
    claim_path = tmp_path / "pair" / f"{phase}.json"
    receipt = _evolved_no_effect_receipt(authority=authority, claim_path=claim_path)

    ledger.reconcile_verified_no_effect_failure(phase, receipt)
    assert ledger.replacement_ready(phase) is True
    assert ledger.evolved_replacement_run_id(phase) == "pair-evolved-2222222222"
    expectation = ledger.replacement_expectation(phase)
    activation = ledger.claim(
        phase,
        authority,
        allow_verified_replacement=True,
        expected_replacement=expectation,
    )
    ledger.terminal(
        phase,
        {"run_id": "pair-evolved-2222222222"},
        replacement_activation=activation,
    )

    payload = __import__("json").loads(claim_path.read_text())
    assert payload["failed_attempts"][0]["outcome"] == ("terminal_evolved_candidate_no_effect")
    assert verified_no_effect_attempt_count(payload, pair_id="pair", phase=phase) == 1


def test_evolved_candidate_replacement_refuses_non_closed_predicate_map(tmp_path):
    ledger = VerifiedReplacementPhaseLedger(tmp_path, pair_id="pair")
    authority = {
        "task_id": "task",
        "artifact_count": 3,
        "artifact_ids_by_type": {
            "text_memory": "memory",
            "skill_bundle": "skill",
            "agent_system": "agent",
        },
    }
    ledger.claim("evolved_candidate", authority)
    claim_path = tmp_path / "pair" / "evolved_candidate.json"
    receipt = _evolved_no_effect_receipt(authority=authority, claim_path=claim_path)
    receipt["no_effect_predicates"].pop("trajectory_has_zero_traces")
    receipt["no_effect_predicates"]["invented_self_attestation"] = True

    with pytest.raises(ValueError):
        ledger.reconcile_verified_no_effect_failure("evolved_candidate", receipt)
    assert __import__("json").loads(claim_path.read_text())["status"] == "claimed"


def test_replacement_activation_is_atomic_across_processes_and_owner_bound(tmp_path):
    authority = {
        "task_id": "task",
        "artifact_count": 3,
        "artifact_ids_by_type": {
            "text_memory": "memory",
            "skill_bundle": "skill",
            "agent_system": "agent",
        },
    }
    ledger = VerifiedReplacementPhaseLedger(tmp_path, pair_id="pair")
    ledger.claim("evolved_candidate", authority)
    claim_path = tmp_path / "pair" / "evolved_candidate.json"
    ledger.reconcile_verified_no_effect_failure(
        "evolved_candidate",
        _evolved_no_effect_receipt(authority=authority, claim_path=claim_path),
    )
    expectation = ledger.replacement_expectation("evolved_candidate")

    context = multiprocessing.get_context("fork")
    start_event = context.Event()
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_concurrent_replacement_activation,
            args=(tmp_path, start_event, result_queue, authority, expectation),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    start_event.set()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    results = [result_queue.get(timeout=2) for _ in processes]
    activated = [result for result in results if result[0] == "activated"]
    rejected = [result for result in results if result[0] == "rejected"]
    assert len(activated) == 1
    assert len(rejected) == 1
    activation = activated[0][1]
    assert len(activation.owner_token) == 64
    assert activation.expectation == expectation
    claimed = json.loads(claim_path.read_text())
    assert claimed["status"] == "claimed"
    assert claimed["replacement_activated"] is True

    with pytest.raises(ValueError, match="owner token"):
        ledger.terminal(
            "evolved_candidate",
            {"run_id": "pair-evolved-2222222222"},
            replacement_activation=activation.__class__(
                owner_token="0" * 64,
                expectation=activation.expectation,
            ),
        )
    assert json.loads(claim_path.read_text())["status"] == "claimed"
    ledger.terminal(
        "evolved_candidate",
        {"run_id": "pair-evolved-2222222222"},
        replacement_activation=activation,
    )
    with pytest.raises(ValueError, match="already terminal"):
        ledger.claim(
            "evolved_candidate",
            authority,
            allow_verified_replacement=True,
        )
    assert json.loads(claim_path.read_text())["status"] == "terminal"


def test_evolved_replacement_rejects_cross_generation_aba_and_stale_run_terminal(
    tmp_path,
):
    authority = {
        "task_id": "task",
        "artifact_count": 3,
        "artifact_ids_by_type": {
            "text_memory": "memory",
            "skill_bundle": "skill",
            "agent_system": "agent",
        },
    }
    ledger = VerifiedReplacementPhaseLedger(tmp_path, pair_id="pair")
    ledger.claim("evolved_candidate", authority)
    claim_path = tmp_path / "pair" / "evolved_candidate.json"
    first_receipt = _evolved_no_effect_receipt(
        authority=authority,
        claim_path=claim_path,
    )
    ledger.reconcile_verified_no_effect_failure("evolved_candidate", first_receipt)
    stale_expectation = ledger.replacement_expectation("evolved_candidate")
    first_activation = ledger.claim(
        "evolved_candidate",
        authority,
        allow_verified_replacement=True,
        expected_replacement=stale_expectation,
    )
    assert first_activation is not None

    second_receipt = deepcopy(first_receipt)
    second_receipt.update(
        {
            "attempt_ordinal": 2,
            "original_claim_sha256": file_sha256(claim_path),
            "active_attempt_run_id": "pair-evolved-2222222222",
            "core_task_id": "pair-evolved-2222222222",
            "replacement_run_id": "pair-evolved-3333333333",
            "core_session_id_sha256": "a" * 64,
            "core_completion_sha256": "b" * 64,
        }
    )
    second_receipt["injection_authority"]["context_id"] = "context-attempt-2"
    ledger.reconcile_verified_no_effect_failure("evolved_candidate", second_receipt)
    current_expectation = ledger.replacement_expectation("evolved_candidate")
    assert stale_expectation.replacement_run_id == "pair-evolved-2222222222"
    assert current_expectation.replacement_run_id == "pair-evolved-3333333333"

    with pytest.raises(AmbiguousPhaseClaimError, match="generation differs"):
        ledger.claim(
            "evolved_candidate",
            authority,
            allow_verified_replacement=True,
            expected_replacement=stale_expectation,
        )
    assert json.loads(claim_path.read_text())["status"] == "replacement_ready"

    current_activation = ledger.claim(
        "evolved_candidate",
        authority,
        allow_verified_replacement=True,
        expected_replacement=current_expectation,
    )
    with pytest.raises(ValueError, match="run authority"):
        ledger.terminal(
            "evolved_candidate",
            {"run_id": "pair-evolved-2222222222"},
            replacement_activation=current_activation,
        )
    assert json.loads(claim_path.read_text())["status"] == "claimed"
    ledger.terminal(
        "evolved_candidate",
        {"run_id": "pair-evolved-3333333333"},
        replacement_activation=current_activation,
    )
    assert json.loads(claim_path.read_text())["status"] == "terminal"
