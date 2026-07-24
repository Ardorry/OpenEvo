from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from dataclasses import fields
from pathlib import Path

import pytest

import openevo_chembench.taskwise_canary_receipt_v1 as receipt_module
from openevo_chembench.taskwise_canary_receipt_v1 import (
    CANARY_EVIDENCE_SCOPE,
    PERFORMANCE_GATE_DISCLAIMER,
    TaskwiseCanaryFindingV1,
    TaskwisePairedCanaryReceiptInputsV1,
    TaskwisePairedCanaryReceiptV1,
    TaskwisePilotAuthorizationV1,
    default_taskwise_canary_receipt_inputs_v1,
    default_taskwise_canary_receipt_path_v1,
    recompute_taskwise_paired_canary_receipt_v1,
    verify_taskwise_paired_canary_receipt_v1,
    write_taskwise_paired_canary_receipt_v1,
)
from openevo_chembench.taskwise_config_v1 import load_taskwise_config_v1


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_GENERATION_ID = f"gen_{'1' * 64}"


def _inputs(tmp_path: Path) -> TaskwisePairedCanaryReceiptInputsV1:
    return TaskwisePairedCanaryReceiptInputsV1(
        repository_root=tmp_path / "repository",
        package_root=tmp_path / "package",
        dataset_root=tmp_path / "dataset",
        source_manifest_path=tmp_path / "source.json",
        framework_lock_path=tmp_path / "framework-lock.json",
        control_config_path=tmp_path / "control.yaml",
        online_config_path=tmp_path / "online.yaml",
    )


def _positive_receipt(*, marker: str = "a") -> TaskwisePairedCanaryReceiptV1:
    return TaskwisePairedCanaryReceiptV1(
        fields={
            "source_commit": marker * 40,
            "pilot_binding_sha256": marker * 64,
            "paired_canary_generation_id": _GENERATION_ID,
            "control_completion_count": 27,
            "online_completion_count": 27,
            "online_core_job_count": 18,
            "online_core_artifact_count": 18,
            "control_cleanup_complete_count": 27,
            "online_cleanup_complete_count": 27,
            "control_cleanup_residual_root_count": 0,
            "online_cleanup_residual_root_count": 0,
            "control_context_binding_violation_count": 0,
            "online_context_binding_violation_count": 0,
            "control_artifact_chain_violation_count": 0,
            "online_artifact_chain_violation_count": 0,
            "online_reflector_cleanup_complete_count": 18,
        },
        finding_codes=(),
        created_at_utc="2026-07-24T00:00:00+00:00",
    )


def test_receipt_inputs_are_path_authorities_not_caller_claims(tmp_path: Path) -> None:
    names = {item.name for item in fields(TaskwisePairedCanaryReceiptInputsV1)}
    assert names == {
        "repository_root",
        "package_root",
        "dataset_root",
        "source_manifest_path",
        "framework_lock_path",
        "control_config_path",
        "online_config_path",
    }
    assert not {
        "passed",
        "verified",
        "paid_pilot_allowed",
        "security_verified",
    }.intersection(names)
    with pytest.raises(TypeError, match="pathlib.Path"):
        TaskwisePairedCanaryReceiptInputsV1(
            repository_root="repository",  # type: ignore[arg-type]
            package_root=tmp_path,
            dataset_root=tmp_path,
            source_manifest_path=tmp_path,
            framework_lock_path=tmp_path,
            control_config_path=tmp_path,
            online_config_path=tmp_path,
        )


def test_receipt_reserved_decision_fields_cannot_be_spoofed() -> None:
    for key in (
        "paid_pilot_allowed",
        "finding_codes",
        "evidence_digest",
        "canary_evidence_scope",
        "performance_gate_disclaimer",
    ):
        with pytest.raises(ValueError, match="reserved"):
            TaskwisePairedCanaryReceiptV1(
                fields={key: True},
                finding_codes=(),
                created_at_utc="2026-07-24T00:00:00+00:00",
            )


def test_pilot_authorization_constructor_is_not_publicly_forgeable() -> None:
    with pytest.raises(TypeError, match="issued only"):
        TaskwisePilotAuthorizationV1(
            receipt_sha256="a" * 64,
            evidence_digest="b" * 64,
            source_commit="c" * 40,
            pilot_binding_sha256="d" * 64,
            paired_canary_generation_id=_GENERATION_ID,
        )


def test_positive_receipt_requires_exact_recomputation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _inputs(tmp_path)
    stored = _positive_receipt()
    path = tmp_path / "receipt.json"
    path.write_bytes(stored.canonical_bytes())
    path.chmod(0o600)
    monkeypatch.setattr(
        receipt_module,
        "recompute_taskwise_paired_canary_receipt_v1",
        lambda actual: stored if actual is inputs else pytest.fail("wrong inputs"),
    )

    authorization = verify_taskwise_paired_canary_receipt_v1(inputs, path)

    assert authorization.receipt_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert authorization.evidence_digest == stored.evidence_digest
    assert authorization.source_commit == "a" * 40
    assert authorization.pilot_binding_sha256 == "a" * 64
    assert authorization.paired_canary_generation_id == _GENERATION_ID
    directly_verified = stored.to_payload()["directly_verified_fields"]
    assert "control_cleanup_residual_root_count" in directly_verified
    assert "online_cleanup_residual_root_count" in directly_verified
    assert "control_context_binding_violation_count" in directly_verified
    assert "online_context_binding_violation_count" in directly_verified
    assert "control_artifact_chain_violation_count" in directly_verified
    assert "online_artifact_chain_violation_count" in directly_verified


def test_receipt_evidence_drift_is_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _inputs(tmp_path)
    stored = _positive_receipt(marker="a")
    path = tmp_path / "receipt.json"
    path.write_bytes(stored.canonical_bytes())
    path.chmod(0o600)
    monkeypatch.setattr(
        receipt_module,
        "recompute_taskwise_paired_canary_receipt_v1",
        lambda _inputs: _positive_receipt(marker="b"),
    )

    with pytest.raises(RuntimeError, match="no longer matches"):
        verify_taskwise_paired_canary_receipt_v1(inputs, path)


def test_blocked_recomputation_is_never_frozen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _inputs(tmp_path)
    blocked = TaskwisePairedCanaryReceiptV1(
        fields={"source_commit": "a" * 40},
        finding_codes=(TaskwiseCanaryFindingV1.CANARY_RUN_INVALID,),
        created_at_utc="2026-07-24T00:00:00+00:00",
    )
    monkeypatch.setattr(
        receipt_module,
        "recompute_taskwise_paired_canary_receipt_v1",
        lambda _inputs: blocked,
    )
    path = tmp_path / "receipt.json"

    with pytest.raises(RuntimeError, match="findings block"):
        write_taskwise_paired_canary_receipt_v1(inputs, path)

    assert not path.exists()


def test_default_receipt_location_is_outside_results_and_private_manifests() -> None:
    inputs = default_taskwise_canary_receipt_inputs_v1(PACKAGE_ROOT)
    receipt_path = default_taskwise_canary_receipt_path_v1(
        PACKAGE_ROOT,
        _GENERATION_ID,
    )
    assert inputs.control_config_path.name == ("control_canary9_taskwise_online_v1.yaml")
    assert inputs.online_config_path.name == "online_canary9_taskwise_online_v1.yaml"
    assert receipt_path.relative_to(PACKAGE_ROOT).as_posix() == (
        "state/taskwise_online_v1/canary9/generations/"
        f"{_GENERATION_ID}/paired_canary_receipt_v1.json"
    )
    assert "results" not in receipt_path.parts
    assert "private_manifests" not in receipt_path.parts


def test_current_incomplete_or_dirty_evidence_cannot_authorize_paid_pilot() -> None:
    receipt = recompute_taskwise_paired_canary_receipt_v1(
        default_taskwise_canary_receipt_inputs_v1(PACKAGE_ROOT)
    )
    payload = receipt.to_payload()
    encoded = json.dumps(payload, sort_keys=True)

    assert receipt.paid_pilot_allowed is False
    assert receipt.finding_codes
    assert payload["paid_pilot_allowed"] is False
    assert payload["canary_evidence_scope"] == CANARY_EVIDENCE_SCOPE
    assert payload["performance_gate_disclaimer"] == PERFORMANCE_GATE_DISCLAIMER
    assert "target" not in encoded.casefold()
    assert "raw_completion" not in encoded.casefold()


def test_success_gate_rejects_any_private_core_failure_receipt(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    config = replace(
        load_taskwise_config_v1(
            PACKAGE_ROOT / "configs" / "online_canary9_taskwise_online_v1.yaml"
        ),
        run_name="online_failure_diagnostic_test",
    )
    failure_root = (
        inputs.package_root
        / "state"
        / "taskwise_online_v1"
        / config.scope
        / config.run_name
        / "private_core_failure_diagnostics"
    )
    failure_root.mkdir(parents=True, mode=0o700)
    receipt = failure_root / f"taskwise_core_failure_{'a' * 32}.json"
    receipt.write_text("{}\n", encoding="utf-8")
    receipt.chmod(0o600)

    with pytest.raises(receipt_module._CoreEvidenceError) as captured:
        receipt_module._recompute_core_evidence(
            inputs=inputs,
            config=config,
            registry=object(),
            public_rows=(),
            private_rows=(),
        )

    assert captured.value.findings == frozenset(
        {TaskwiseCanaryFindingV1.CORE_FAILURE_DIAGNOSTICS_PRESENT}
    )
