from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from openevo_chembench.chembench4k_models import CHEMBENCH4K_CATEGORIES
from openevo_chembench.supervised_transfer_v2.reporting import (
    _mcnemar_exact,
    _paired_metrics,
    build_reports_v2,
)
from openevo_chembench.supervised_transfer_v2.test_ledger import (
    FinalTestConsumptionLedgerV2,
    FinalTestLedgerError,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _ledger(path: Path, *, config: str = "config") -> FinalTestConsumptionLedgerV2:
    return FinalTestConsumptionLedgerV2(
        path=path,
        source_commit="a" * 40,
        artifact_set_digest=_sha("artifact-set"),
        config_digest=_sha(config),
        model_digest=_sha("model"),
    )


def test_final_test_ledger_allows_recovery_only_before_completion(
    tmp_path: Path,
) -> None:
    path = (tmp_path / "private/final-test-ledger.jsonl").resolve()
    ledger = _ledger(path)
    uid = _sha("test-item")

    ledger.claim(task_uid=uid, arm="control", attempt_id="attempt-0001", timestamp="t1")
    ledger.claim(task_uid=uid, arm="control", attempt_id="attempt-0002", timestamp="t2")
    ledger.complete(
        task_uid=uid,
        arm="control",
        attempt_id="attempt-0002",
        completion="A",
        timestamp="t3",
    )

    with pytest.raises(FinalTestLedgerError, match="TEST_ITEM_ALREADY_COMPLETED"):
        ledger.claim(
            task_uid=uid,
            arm="control",
            attempt_id="attempt-0003",
            timestamp="t4",
        )
    assert ledger.completed(uid, "control")
    assert ledger.summary()["completion_count"] == 1


def test_final_test_ledger_recovery_rejects_identity_drift(tmp_path: Path) -> None:
    path = (tmp_path / "private/final-test-ledger.jsonl").resolve()
    ledger = _ledger(path)
    ledger.claim(
        task_uid=_sha("test-item"),
        arm="evolved",
        attempt_id="attempt-0001",
        timestamp="t1",
    )

    with pytest.raises(FinalTestLedgerError, match="TEST_LEDGER_IDENTITY_MISMATCH"):
        _ledger(path, config="different-config")


def test_protocol_global_ledger_blocks_a_fresh_run_after_completion(
    tmp_path: Path,
) -> None:
    path = (tmp_path / "state/private/final-test-ledger.jsonl").resolve()
    uid = _sha("globally-consumed-item")
    first_run = _ledger(path)
    first_run.claim(
        task_uid=uid,
        arm="control",
        attempt_id="first-run-attempt",
        timestamp="t1",
    )
    first_run.complete(
        task_uid=uid,
        arm="control",
        attempt_id="first-run-attempt",
        completion="D",
        timestamp="t2",
    )

    fresh_run = _ledger(path)
    with pytest.raises(FinalTestLedgerError, match="TEST_ITEM_ALREADY_COMPLETED"):
        fresh_run.claim(
            task_uid=uid,
            arm="control",
            attempt_id="second-run-attempt",
            timestamp="t3",
        )


def test_paired_statistics_are_exact_and_deterministic() -> None:
    rows = [
        {
            "control_correct": control,
            "evolved_correct": evolved,
            "control_strict_parse": 1,
            "evolved_strict_parse": 1,
        }
        for control, evolved in ((0, 1), (0, 1), (1, 0), (1, 1), (0, 0))
    ]
    first = _paired_metrics(rows)
    second = _paired_metrics(rows)

    assert first == second
    assert first["wrong_to_correct"] == 2
    assert first["correct_to_wrong"] == 1
    assert first["both_correct"] == 1
    assert first["both_wrong"] == 1
    assert first["mcnemar_exact_p"] == _mcnemar_exact(2, 1) == 1.0


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_complete_synthetic_report_builds_without_model_or_test_data(
    tmp_path: Path,
) -> None:
    private_rows: list[dict[str, object]] = []
    public_rows: list[dict[str, object]] = []
    task_ordinal = 0
    for category in CHEMBENCH4K_CATEGORIES:
        for category_index in range(50):
            task_ordinal += 1
            uid = _sha(f"synthetic-{category}-{category_index}")
            for logical_arm in ("control_train", "online_train"):
                for round_index in range(4):
                    private_rows.append(
                        {
                            "kind": "PRIVATE_EVALUATED",
                            "logical_arm": logical_arm,
                            "task_uid": uid,
                            "category": category,
                            "task_ordinal": task_ordinal,
                            "round_index": round_index,
                            "official_prediction": "A",
                            "strict_prediction": "A",
                            "correct": (category_index + round_index) % 3 != 0,
                            "strict_parse_status": "parsed",
                        }
                    )
            private_rows.extend(
                (
                    {
                        "kind": "PRIVATE_EVALUATED",
                        "logical_arm": "test_control",
                        "task_uid": uid,
                        "category": category,
                        "official_prediction": "A",
                        "correct": category_index % 2 == 0,
                        "strict_parse_status": "parsed",
                    },
                    {
                        "kind": "PRIVATE_EVALUATED",
                        "logical_arm": "test_evolved",
                        "task_uid": uid,
                        "category": category,
                        "official_prediction": "B",
                        "correct": category_index % 3 != 0,
                        "strict_parse_status": "parsed",
                    },
                )
            )
            for cycle in range(1, 4):
                public_rows.append(
                    {
                        "kind": "REFLECTOR_SUPERVISED",
                        "stage": "ONLINE_TRAIN",
                        "category": category,
                        "task_index": category_index,
                        "cycle": cycle,
                        "global_update_ordinal": category_index * 3 + cycle,
                        "text_memory_bytes": 100 + category_index,
                        "skill_bytes": 120 + category_index,
                        "agent_system_bytes": 80 + category_index,
                        "confirmed_rules": category_index // 2,
                        "provisional_rules": 1,
                        "retired_rules": 0,
                        "failure_modes": 0,
                    }
                )

    result_root = tmp_path / "results/run"
    state_root = tmp_path / "state/run"
    _write_jsonl(state_root / "private/events.jsonl", private_rows)
    _write_jsonl(result_root / "public/events.jsonl", public_rows)
    result = build_reports_v2(
        repository_root=tmp_path,
        result_root=result_root,
        state_root=state_root,
        run_id="synthetic-report-run",
    )

    report_root = Path(str(result["report_root"]))
    assert result["status"] == "PASS"
    assert (report_root / "final_report.md").is_file()
    assert (report_root / "final_report.html").is_file()
    assert (report_root / "integrity_receipt.json").is_file()
    assert (report_root / "test_paired_results.csv").read_text().count("\n") == 451
