import json

import pytest

from openevo_chemcrow.hashing import canonical_sha256
from openevo_chemcrow.submission import collect_submission_replacements


def ledger(root):
    pair = "experiment--chemcrow-13"
    phase = "baseline_candidate"
    authority = {"task_id": "chemcrow-13"}
    proof = {
        "schema_version": "chemcrow_pre_candidate_no_effect_recovery_v1",
        "status": "VERIFIED_NO_CANDIDATE_EFFECT_REPLACEMENT_READY",
        "pair_id": pair,
        "phase": phase,
        "attempt_ordinal": 1,
        "authority_sha256": canonical_sha256(authority),
        "candidate_model_call_proven_absent": True,
        "duplicate_scientific_call": False,
        "recorded_before_replacement_dispatch": True,
        "no_effect_predicates": {"proof": True},
        "core_task_id": "failed-call",
    }
    terminal = {"run_id": "replacement-call"}
    claim = {
        "active_attempt_ordinal": 2,
        "replacement_activated": True,
        "replacement_activation_token_sha256": "f" * 64,
        "status": "terminal",
        "phase": phase,
        "authority": authority,
        "authority_sha256": canonical_sha256(authority),
        "receipt": terminal,
        "receipt_sha256": canonical_sha256(terminal),
        "failed_attempts": [
            {
                "attempt_ordinal": 1,
                "outcome": "terminal_pre_candidate_no_effect",
                "receipt": proof,
                "receipt_sha256": canonical_sha256(proof),
            }
        ],
    }
    path = root / pair / f"{phase}.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(claim))
    return path, proof


def test_exports_real_replacement_identity_and_proof_hash(tmp_path):
    _, proof = ledger(tmp_path)
    result = collect_submission_replacements(
        tmp_path,
        "experiment",
        ["chemcrow-13"],
        {"verified_pre_candidate_no_effect_attempt_count": 1},
    )
    assert len(result) == 1
    assert result[0].failed_call_id == "failed-call"
    assert result[0].replacement_call_id == "replacement-call"
    assert result[0].authority_sha256 == canonical_sha256(proof)


def test_missing_ledger_cannot_hide_audited_replacements(tmp_path):
    with pytest.raises(ValueError, match="replacement counts differ"):
        collect_submission_replacements(
            tmp_path,
            "experiment",
            ["chemcrow-13"],
            {"verified_pre_candidate_no_effect_attempt_count": 1},
        )


@pytest.mark.parametrize("mutation", ["proof", "terminal", "claimed"])
def test_rejects_changed_proof_or_unsealed_terminal(tmp_path, mutation):
    path, _ = ledger(tmp_path)
    claim = json.loads(path.read_text())
    if mutation == "proof":
        claim["failed_attempts"][0]["receipt"]["core_task_id"] = "tampered"
    elif mutation == "terminal":
        claim["receipt"]["run_id"] = "tampered"
    else:
        claim["status"] = "claimed"
    path.write_text(json.dumps(claim))
    with pytest.raises(ValueError):
        collect_submission_replacements(
            tmp_path,
            "experiment",
            ["chemcrow-13"],
            {"verified_pre_candidate_no_effect_attempt_count": 1},
        )


def test_legacy_zero_replacement_audit_remains_compatible(tmp_path):
    assert collect_submission_replacements(tmp_path, "experiment", ["chemcrow-13"], {}) == []
