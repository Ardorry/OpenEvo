from __future__ import annotations

from pathlib import Path

import pytest

from openevo_researchclawbench.training_control import (
    DurableTrainingControl,
    TrainingOperationsUnavailable,
)


class _Config:
    def __init__(self, root: Path) -> None:
        self.experiment_root = root
        self.path = root / "protocol.yaml"
        self.path.write_text("protocol: frozen\n", encoding="utf-8")

    def require(self, key: str) -> str:
        values = {
            "source_identity.openevo_core_source_tree_sha256": "2" * 64,
            "source_identity.adapter_tree_sha256": "3" * 64,
            "budgets.max_candidate_model_calls": "51",
            "budgets.max_reflector_model_calls": "102",
            "budgets.cumulative_runtime_seconds": "345600",
            "judge.runs_per_attempt": "1",
        }
        return values[key]


def test_control_status_is_read_only_when_state_is_absent(tmp_path: Path) -> None:
    config = _Config(tmp_path)
    run_root = tmp_path / "supervisor" / "rcb_oe_v0_missing"
    with pytest.raises(ValueError, match="does not exist"):
        DurableTrainingControl(
            config=config,
            run_id="rcb_oe_v0_missing",
            require_existing=True,
        )
    assert not run_root.exists()


def test_control_initializes_verifies_and_refuses_unbound_mutation(
    tmp_path: Path,
) -> None:
    config = _Config(tmp_path)
    control = DurableTrainingControl(
        config=config,
        run_id="rcb_oe_v0_life_three_attempt",
    )
    assert control.initialize()["stage"] == "INITIALIZED"

    restarted = DurableTrainingControl(
        config=config,
        run_id="rcb_oe_v0_life_three_attempt",
        require_existing=True,
    )
    assert restarted.verify()["status"] == "PASS"
    restarted.run_next()
    blocked = restarted.run_next()
    assert blocked["stage"] == "BLOCKED"
    assert blocked["failure_reason"] == "CORE_CONTROL_PREFLIGHT_FAILED"
    assert "active_candidate_intent" not in blocked


def test_control_rejects_foreign_run_namespace(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="rcb_oe_v0"):
        DurableTrainingControl(config=_Config(tmp_path), run_id="another-run")
