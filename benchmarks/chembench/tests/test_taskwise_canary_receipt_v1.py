from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

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
from openevo_chembench.reflector_execution_boundary_v2 import (
    ReflectorBoundaryStatusV2,
    ReflectorExecutionReceiptV2,
    TASKWISE_SOURCE_SPLIT,
)


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


def _write_reflector_receipt(
    tmp_path: Path,
    *,
    status: ReflectorBoundaryStatusV2 = ReflectorBoundaryStatusV2.COMPLETED,
    codex_returncode: int = 0,
    last_message_sha256: str | None = "d" * 64,
    stderr_tail_codes: tuple[str, ...] = (),
) -> tuple[Path, SimpleNamespace]:
    audit_root = tmp_path / "private_reflector_events"
    invocation_id = "a" * 32
    invocation_root = audit_root / invocation_id
    invocation_root.mkdir(parents=True)
    event_raw = b'{"type":"assistant_message"}\n'
    event_sha256 = hashlib.sha256(event_raw).hexdigest()
    event_path = invocation_root / "events.jsonl"
    event_path.write_bytes(event_raw)
    event_path.chmod(0o600)
    receipt = ReflectorExecutionReceiptV2(
        invocation_id=invocation_id,
        status=status,
        mechanism="bubblewrap",
        wrapper_invoked=True,
        real_codex_sha256="1" * 64,
        dev_artifact_sha256="2" * 64,
        ordered_records_sha256="3" * 64,
        record_count=1,
        event_stream_sha256=event_sha256,
        event_counts=(),
        private_event_reference=f"{invocation_id}/events.jsonl",
        last_message_sha256=last_message_sha256,
        cleanup_complete=True,
        retry_allowed=False,
        resume_allowed=False,
        replacement_completion_allowed=False,
        codex_returncode=codex_returncode,
        stderr_sha256=hashlib.sha256(b"redacted stderr").hexdigest(),
        stderr_tail_codes=stderr_tail_codes,
        protocol_id="taskwise_online_evolution_v1",
        source_split=TASKWISE_SOURCE_SPLIT,
    )
    receipt_path = invocation_root / "receipt.json"
    receipt_path.write_text(
        json.dumps(receipt.to_payload(), sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    receipt_path.chmod(0o600)
    result = SimpleNamespace(
        reflector_execution_receipt_sha256=receipt.digest,
        update_index=1,
        reflector_input_digest=receipt.ordered_records_sha256,
        reflector_event_stream_sha256=receipt.event_stream_sha256,
    )
    return audit_root, result


def test_completed_reflector_with_recovered_timeout_warning_is_accepted(
    tmp_path: Path,
) -> None:
    audit_root, result = _write_reflector_receipt(
        tmp_path,
        stderr_tail_codes=("CODEX_TIMEOUT",),
    )

    digest = receipt_module._verify_reflector_receipts(audit_root, (result,))

    assert len(digest) == 64


@pytest.mark.parametrize(
    "stderr_code",
    ("CODEX_AUTH_ERROR", "CODEX_CONFIG_PARSE_ERROR", "CODEX_STDERR_REDACTED"),
)
def test_completed_reflector_rejects_nonrecoverable_stderr_codes(
    tmp_path: Path,
    stderr_code: str,
) -> None:
    audit_root, result = _write_reflector_receipt(
        tmp_path,
        stderr_tail_codes=(stderr_code,),
    )

    with pytest.raises(receipt_module._CoreEvidenceError) as captured:
        receipt_module._verify_reflector_receipts(audit_root, (result,))

    assert captured.value.findings == frozenset(
        {TaskwiseCanaryFindingV1.REFLECTOR_SECURITY_INVALID}
    )


@pytest.mark.parametrize(
    ("status", "returncode", "last_message"),
    (
        (ReflectorBoundaryStatusV2.CODEX_FAILED, 0, "d" * 64),
        (ReflectorBoundaryStatusV2.COMPLETED, 1, "d" * 64),
        (ReflectorBoundaryStatusV2.COMPLETED, 0, None),
    ),
)
def test_timeout_warning_does_not_override_other_reflector_failures(
    tmp_path: Path,
    status: ReflectorBoundaryStatusV2,
    returncode: int,
    last_message: str | None,
) -> None:
    audit_root, result = _write_reflector_receipt(
        tmp_path,
        status=status,
        codex_returncode=returncode,
        last_message_sha256=last_message,
        stderr_tail_codes=("CODEX_TIMEOUT",),
    )

    with pytest.raises(receipt_module._CoreEvidenceError) as captured:
        receipt_module._verify_reflector_receipts(audit_root, (result,))

    assert captured.value.findings == frozenset(
        {TaskwiseCanaryFindingV1.REFLECTOR_SECURITY_INVALID}
    )
