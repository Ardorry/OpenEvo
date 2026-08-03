from __future__ import annotations

import json
from pathlib import Path

import pytest

from openevo_chembench.supervised_transfer_v1.common import canonical_json_bytes
from openevo_chembench.temperature_full_evolve_v1 import formal_runtime as base_runtime
from openevo_chembench.temperature_safe_evolve_v2 import campaign as campaign_module
from openevo_chembench.temperature_safe_evolve_v2 import formal_runtime
from openevo_chembench.temperature_safe_evolve_v2.campaign import (
    SafeEvolveCampaignV2,
    _classify_four_folds,
)
from openevo_chembench.temperature_safe_evolve_v2.fold_run import (
    FoldRunOutcomeV2,
    SafeFoldRunnerV2,
)
from openevo_chembench.temperature_safe_evolve_v2.runtime_services import (
    fold_runtime_service_run_id_v2,
)


def _outcome(
    tmp_path: Path,
    index: int,
    *,
    positive: int,
    negative: int,
    promotion: int,
    deployed: bool,
) -> FoldRunOutcomeV2:
    g0 = [True] * 40 + [False] * 10
    raw = list(g0)
    for offset in range(positive):
        raw[40 + offset] = True
    for offset in range(negative):
        raw[offset] = False
    deployed_values = raw if deployed else g0
    path = tmp_path / f"r{index}.json"
    path.write_bytes(
        canonical_json_bytes(
            {
                "test": {
                    "g0_correctness": g0,
                    "raw_correctness": raw,
                    "deployed_correctness": deployed_values,
                }
            }
        )
    )
    delta = 100.0 * (sum(raw) - sum(g0)) / 50
    metrics = {
        "raw_vs_g0": {"delta_percentage_points": delta},
        "parser_retry_failure_parity": True,
    }
    return FoldRunOutcomeV2(
        fold_id=f"R{index}",
        run_id=f"run-{index}",
        promotion_count=promotion,
        retirement_count=0,
        deployment_real_evolved=deployed,
        test_metrics=metrics,
        public_summary={},
        private_result_path=path,
        r0_continue=None,
        r0_stop_reasons=(),
    )


def test_four_fold_classification_obeys_preregistered_order(tmp_path: Path) -> None:
    go = tuple(
        _outcome(tmp_path, index, positive=1, negative=0, promotion=1, deployed=index < 2)
        for index in range(4)
    )
    assert _classify_four_folds(go) == "COMPLETE_GO_MECHANISM_SIGNAL"

    safe = tuple(
        _outcome(
            tmp_path,
            index + 4,
            positive=1 if index == 0 else 0,
            negative=0,
            promotion=1 if index == 0 else 0,
            deployed=index == 0,
        )
        for index in range(4)
    )
    assert _classify_four_folds(safe) == "COMPLETE_SAFE_FALLBACK_ONLY"

    no_go = tuple(
        _outcome(tmp_path, index + 8, positive=0, negative=0, promotion=0, deployed=False)
        for index in range(4)
    )
    assert _classify_four_folds(no_go) == "COMPLETE_NO_GO_SAFE_EVOLVE_V2"


def test_v2_formal_runtime_adapter_uses_distinct_dynamic_receipt_name() -> None:
    formal_runtime._configure()
    assert base_runtime.FORMAL_RUNTIME_SCHEMA == "TemperatureSafeEvolveFormalRuntimeReceiptV2"
    assert base_runtime.FORMAL_RUNTIME_ROOT_RELATIVE.endswith("/formal_runtime_v1")
    assert Path(base_runtime.FORMAL_RUNTIME_RECEIPT_RELATIVE).name == (
        "formal_runtime_receipt_v2.json"
    )
    assert "temperature_safe_evolve_v2" in json.dumps(base_runtime._FORMAL_SOURCE_PATHS)


def test_each_fold_gets_a_distinct_deterministic_runtime_service_identity() -> None:
    campaign = "stv3-temperature-safe-evolve-v2-20990101T000000Z"
    values = [
        fold_runtime_service_run_id_v2(campaign_run_id=campaign, fold_id=f"R{index}")
        for index in range(4)
    ]
    assert len(set(values)) == 4
    assert values == [
        fold_runtime_service_run_id_v2(campaign_run_id=campaign, fold_id=f"R{index}")
        for index in range(4)
    ]
    assert all(value.startswith("stv3-temperature-safe-services-20990101T000000Z-") for value in values)


def test_fold_runtime_lifecycle_is_bound_to_empty_inventory_and_stop_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Runtime:
        service_run_id = "stv3-temperature-safe-services-20990101T000000Z-12345678"
        source_commit = "a" * 40
        digest = "b" * 64

        def require_current(self) -> dict[str, object]:
            return {"runtime_services_identity_sha256": self.digest}

    runtime = Runtime()
    monkeypatch.setattr(
        campaign_module,
        "start_temperature_safe_runtime_services_v2",
        lambda **_kwargs: runtime,
    )
    monkeypatch.setattr(
        campaign_module,
        "audit_empty_temperature_safe_runtime_services_v2",
        lambda **_kwargs: {
            "service_run_id": runtime.service_run_id,
            "runtime_services_identity_sha256": runtime.digest,
            "inventory_sha256": "c" * 64,
            "model_calls_observed": 0,
        },
    )
    monkeypatch.setattr(
        campaign_module,
        "stop_temperature_safe_runtime_services_v2",
        lambda **_kwargs: {
            "service_run_id": runtime.service_run_id,
            "runtime_services_identity_sha256": runtime.digest,
            "completion_root_marker_sha256": "d" * 64,
            "completion_evidence_preserved": True,
            "cleanup_complete": True,
        },
    )
    campaign = object.__new__(SafeEvolveCampaignV2)
    campaign.repository = tmp_path
    campaign.run_id = "stv3-temperature-safe-evolve-v2-20990101T000000Z"
    campaign.source_commit = runtime.source_commit
    campaign.root = tmp_path / "campaign"

    started = campaign._start_fold_runtime(fold_id="R0")
    campaign._stop_bound_runtime(scope="r0", runtime=started)

    events = [
        json.loads(line)
        for line in (campaign.root / "private/campaign_events.jsonl").read_text().splitlines()
    ]
    assert [event["kind"] for event in events] == [
        "RUNTIME_SERVICES_STARTED",
        "RUNTIME_SERVICES_STOPPED",
    ]
    assert events[0]["payload"]["model_calls_observed_before_fold"] == 0
    assert events[1]["payload"]["completion_evidence_preserved"] is True


def test_test_integrity_audit_checks_gt_order_and_post_freeze_mutation() -> None:
    runner = object.__new__(SafeFoldRunnerV2)
    before = (
        [{"kind": "REFLECTOR_ACCEPTED", "payload": {}} for _ in range(4)]
        + [{"kind": "CORE_JOB_COMPLETED", "payload": {}} for _ in range(4)]
        + [
            {"kind": "FINAL_STATE_FROZEN", "payload": {}},
            {"kind": "TEST_INFERENCE_CLOSED", "payload": {}},
            {"kind": "TEST_GT_RELEASED", "payload": {}},
        ]
    )
    evaluations = [
        {
            "kind": "CALL_EVALUATED",
            "payload": {"evaluation_json": '{"phase":"test"}'},
        }
        for _ in range(50)
    ]
    closed = {"kind": "FOLD_TEST_CLOSED", "payload": {}}
    block = {
        label: type("Arm", (), {"accepted": (None,) * 50, "evaluations": (None,) * 50})()
        for label in ("g0", "raw")
    }
    ledger = type("Ledger", (), {"events": (*before, *evaluations, closed)})()

    assert runner._test_integrity_audit(ledger=ledger, test_block=block)
    mutated = type(
        "Ledger",
        (),
        {
            "events": (
                *before,
                {"kind": "REFLECTOR_ACCEPTED", "payload": {}},
                *evaluations,
                closed,
            )
        },
    )()
    assert not runner._test_integrity_audit(ledger=mutated, test_block=block)
