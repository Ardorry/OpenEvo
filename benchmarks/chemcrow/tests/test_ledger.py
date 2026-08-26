from __future__ import annotations

import pytest

from openevo_chemcrow.hashing import canonical_sha256, file_sha256
from openevo_chemcrow.ledger import AmbiguousPhaseClaimError, PhaseLedger
from openevo_chemcrow.replacement_ledger import (
    VerifiedReplacementPhaseLedger,
    verified_no_effect_attempt_count,
)


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

    ledger.claim(
        "baseline_candidate",
        authority,
        allow_verified_replacement=True,
    )
    with pytest.raises(AmbiguousPhaseClaimError, match="do not duplicate"):
        ledger.audit_resume()
    ledger.terminal("baseline_candidate", {"run_id": "replacement-run"})
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
