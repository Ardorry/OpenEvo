from __future__ import annotations

import json

import pytest

from openevo_chemcrow.hashing import canonical_sha256
from openevo_chemcrow.ledger import PhaseLedger
from openevo_chemcrow.pre_candidate_recovery import (
    reconcile_pre_candidate_no_effect_failure,
)
from openevo_chemcrow.replacement_ledger import VerifiedReplacementPhaseLedger
from openevo_chemcrow.runtime import CORE_MANAGED_CODEX_ROUTE


def _completion(pair_id: str) -> dict:
    return {
        "session_id": "opaque-session-id",
        "task_id": f"{pair_id}-baseline-0123456789",
        "status": "ERROR",
        "error": (
            "agent execution failed: Codex subscription credential isolation could "
            "not be proven (validation_failed)"
        ),
        "trajectory": {"metadata": {"record_count": 0}, "traces": []},
        "workspace_result": None,
        "metadata": {
            "execution_route": CORE_MANAGED_CODEX_ROUTE,
            "host_codex_exec_forbidden": True,
            "evolution": {"context_artifact_ids": []},
            "openevo": {"credential_isolation": {"policy_sha256": "policy"}},
        },
    }


def test_reconcile_pre_candidate_failure_is_fail_closed_and_deterministic(tmp_path):
    experiment_id = "experiment"
    task_id = "chemcrow-10"
    pair_id = f"{experiment_id}--{task_id}"
    run_root = tmp_path / "run"
    ledger_root = run_root / "claims"
    authority = {
        "task_id": task_id,
        "s0_hash": "s0",
        "artifact_ids": [],
        "artifact_inventory": {},
    }
    PhaseLedger(ledger_root, pair_id=pair_id).claim("baseline_candidate", authority)
    config = tmp_path / "config.yaml"
    config.write_text("experiment: test\n")
    completion = tmp_path / "completion.json"
    completion.write_text(json.dumps(_completion(pair_id)))

    receipt_path, receipt = reconcile_pre_candidate_no_effect_failure(
        config_path=config,
        run_root=run_root,
        ledger_root=ledger_root,
        experiment_id=experiment_id,
        task_id=task_id,
        core_completion_path=completion,
    )
    assert receipt_path.is_file()
    assert receipt["candidate_model_call_proven_absent"] is True
    assert receipt["duplicate_scientific_call"] is False
    assert set(receipt["no_effect_predicates"].values()) == {True}
    claim = json.loads((ledger_root / pair_id / "baseline_candidate.json").read_text())
    assert claim["status"] == "replacement_ready"
    assert claim["failed_attempts"][0]["receipt_sha256"] == canonical_sha256(receipt)


def test_reconcile_rejects_any_nonzero_trajectory(tmp_path):
    experiment_id = "experiment"
    task_id = "chemcrow-10"
    pair_id = f"{experiment_id}--{task_id}"
    run_root = tmp_path / "run"
    ledger_root = run_root / "claims"
    PhaseLedger(ledger_root, pair_id=pair_id).claim(
        "baseline_candidate",
        {"task_id": task_id, "artifact_ids": [], "artifact_inventory": {}},
    )
    config = tmp_path / "config.yaml"
    config.write_text("experiment: test\n")
    payload = _completion(pair_id)
    payload["trajectory"]["metadata"]["record_count"] = 1
    completion = tmp_path / "completion.json"
    completion.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="zero_records"):
        reconcile_pre_candidate_no_effect_failure(
            config_path=config,
            run_root=run_root,
            ledger_root=ledger_root,
            experiment_id=experiment_id,
            task_id=task_id,
            core_completion_path=completion,
        )


def test_reconcile_accepts_distinct_second_no_effect_attempt(tmp_path):
    experiment_id = "experiment"
    task_id = "chemcrow-12"
    pair_id = f"{experiment_id}--{task_id}"
    run_root = tmp_path / "run"
    ledger_root = run_root / "claims"
    authority = {
        "task_id": task_id,
        "s0_hash": "s0",
        "artifact_ids": [],
        "artifact_inventory": {},
    }
    PhaseLedger(ledger_root, pair_id=pair_id).claim("baseline_candidate", authority)
    config = tmp_path / "config.yaml"
    config.write_text("experiment: test\n")
    first_completion = tmp_path / "completion-1.json"
    first_completion.write_text(json.dumps(_completion(pair_id)))
    reconcile_pre_candidate_no_effect_failure(
        config_path=config,
        run_root=run_root,
        ledger_root=ledger_root,
        experiment_id=experiment_id,
        task_id=task_id,
        core_completion_path=first_completion,
    )
    ledger = VerifiedReplacementPhaseLedger(ledger_root, pair_id=pair_id)
    expectation = ledger.replacement_expectation("baseline_candidate")
    ledger.claim(
        "baseline_candidate",
        authority,
        allow_verified_replacement=True,
        expected_replacement=expectation,
    )
    second_payload = _completion(pair_id)
    second_payload["task_id"] = f"{pair_id}-baseline-abcdef0123"
    second_payload["session_id"] = "second-opaque-session-id"
    second_completion = tmp_path / "completion-2.json"
    second_completion.write_text(json.dumps(second_payload))
    receipt_path, receipt = reconcile_pre_candidate_no_effect_failure(
        config_path=config,
        run_root=run_root,
        ledger_root=ledger_root,
        experiment_id=experiment_id,
        task_id=task_id,
        core_completion_path=second_completion,
    )
    assert receipt_path.name == "baseline_candidate.attempt-2.no-effect.json"
    assert receipt["attempt_ordinal"] == 2
    claim = json.loads((ledger_root / pair_id / "baseline_candidate.json").read_text())
    assert claim["status"] == "replacement_ready"
    assert len(claim["failed_attempts"]) == 2
