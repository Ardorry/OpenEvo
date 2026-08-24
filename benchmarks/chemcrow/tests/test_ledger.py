from __future__ import annotations

import pytest

from openevo_chemcrow.ledger import AmbiguousPhaseClaimError, PhaseLedger


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
