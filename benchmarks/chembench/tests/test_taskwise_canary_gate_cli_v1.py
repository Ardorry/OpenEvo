from __future__ import annotations

from pathlib import Path

import pytest

import openevo_chembench.taskwise_cli_v1 as cli


SCRIPTS_ROOT = Path(__file__).resolve().parents[1] / "scripts"
_CURRENT_GENERATION = f"gen_{'1' * 64}"
_OLD_GENERATION = f"gen_{'2' * 64}"


def test_pilot_suite_verifies_receipt_before_state_is_created(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    touched = False

    def forbidden_state(*_args, **_kwargs):
        nonlocal touched
        touched = True
        pytest.fail("suite state was touched before canary authorization")

    monkeypatch.setattr(cli, "_load_or_initialize_suite_state", forbidden_state)
    monkeypatch.setattr(
        cli,
        "require_taskwise_pilot_authorization_v1",
        lambda: (_ for _ in ()).throw(
            cli.TaskwiseCLIError("TASKWISE_PAIRED_CANARY_RECEIPT_INVALID")
        ),
    )

    with pytest.raises(
        cli.TaskwiseCLIError,
        match="TASKWISE_PAIRED_CANARY_RECEIPT_INVALID",
    ):
        cli.run_pilot500_stream_suite(
            arm="control",
            suite_state_path=tmp_path / "suite-state.json",
        )

    assert touched is False
    assert not (tmp_path / "suite-state.json").exists()


def test_direct_pilot_arm_verifies_receipt_before_source_or_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_gate_called = False

    def forbidden_source_gate(_config):
        nonlocal source_gate_called
        source_gate_called = True
        pytest.fail("source gate ran before canary authorization")

    monkeypatch.setattr(
        cli,
        "require_taskwise_pilot_authorization_v1",
        lambda: (_ for _ in ()).throw(
            cli.TaskwiseCLIError("TASKWISE_PAIRED_CANARY_RECEIPT_INVALID")
        ),
    )
    config = cli.CONFIG_ROOT / "control_pilot500_stream_00_taskwise_online_v1.yaml"

    with pytest.raises(
        cli.TaskwiseCLIError,
        match="TASKWISE_PAIRED_CANARY_RECEIPT_INVALID",
    ):
        cli.run_arm(config, source_gate=forbidden_source_gate)

    assert source_gate_called is False


def test_direct_pilot_resume_is_forbidden_before_receipt_or_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt_called = False

    def forbidden_receipt():
        nonlocal receipt_called
        receipt_called = True
        pytest.fail("pilot resume must stop before receipt verification")

    monkeypatch.setattr(
        cli,
        "require_taskwise_pilot_authorization_v1",
        forbidden_receipt,
    )
    config = cli.CONFIG_ROOT / "control_pilot500_stream_00_taskwise_online_v1.yaml"
    with pytest.raises(cli.TaskwiseCLIError, match="PILOT_RESUME_FORBIDDEN"):
        cli.run_arm(config, resume=True)
    assert receipt_called is False


def test_canary_arm_does_not_require_prior_canary_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary_receipt_called = False

    def forbidden_receipt():
        nonlocal canary_receipt_called
        canary_receipt_called = True
        pytest.fail("canary execution cannot require its own future receipt")

    monkeypatch.setattr(
        cli,
        "require_taskwise_pilot_authorization_v1",
        forbidden_receipt,
    )
    monkeypatch.setattr(
        cli,
        "verify_taskwise_source_gate_v1",
        lambda _config: (_ for _ in ()).throw(cli.TaskwiseCLIError("SOURCE_STOP")),
    )

    with pytest.raises(cli.TaskwiseCLIError, match="SOURCE_STOP"):
        cli.run_arm(cli.CONFIG_ROOT / "control_canary9_taskwise_online_v1.yaml")

    assert canary_receipt_called is False


def test_receipt_cli_has_no_caller_claim_or_bypass_flags() -> None:
    with pytest.raises(SystemExit):
        cli.main(["verify-canary-receipt", "--verified"])
    with pytest.raises(SystemExit):
        cli.main(["freeze-canary-receipt", "--paid-pilot-allowed"])


def test_only_current_paired_canary_generation_can_be_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli,
        "current_taskwise_canary_generation_id_v1",
        lambda _inputs: _CURRENT_GENERATION,
    )
    assert cli._current_canary_generation_v1() == _CURRENT_GENERATION
    with pytest.raises(cli.TaskwiseCLIError, match="GENERATION_ID_INVALID"):
        cli._current_canary_generation_v1(_OLD_GENERATION)


def test_canary_compare_freezes_then_immediately_verifies_receipt() -> None:
    script = (SCRIPTS_ROOT / "compare_canary9_taskwise_online_v1.sh").read_text(encoding="utf-8")
    compare_at = script.index("run_taskwise_cli compare --scope canary9")
    freeze_at = script.index("run_taskwise_cli freeze-canary-receipt")
    verify_at = script.index("run_taskwise_cli verify-canary-receipt")
    assert compare_at < freeze_at < verify_at


@pytest.mark.parametrize(
    "script_name",
    (
        "run_control_pilot500_streams_taskwise_online_v1.sh",
        "run_online_pilot500_streams_taskwise_online_v1.sh",
    ),
)
def test_pilot_entrypoints_verify_receipt_before_suite(
    script_name: str,
) -> None:
    script = (SCRIPTS_ROOT / script_name).read_text(encoding="utf-8")
    assert script.index("run_taskwise_cli verify-canary-receipt") < script.index(
        "run_taskwise_cli run-pilot-stream-suite"
    )
