from __future__ import annotations

from dataclasses import fields as dataclass_fields
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import openevo_chembench.benchmark_receipt_v2 as receipt_module
from openevo_chembench.benchmark_receipt_v2 import (
    DISCLAIMER,
    PRODUCT_DISCLAIMER,
    BenchmarkAuthorizationV2,
    BenchmarkExecutionReceiptV2,
    BenchmarkReceiptInputsV2,
    ReceiptFindingV2,
    recompute_benchmark_receipt_v2,
    verify_receipt_and_issue_authorization_v2,
    write_benchmark_receipt_v2,
)
from openevo_chembench.reflector_execution_boundary_v2 import (
    ReflectorBoundaryStatusV2,
    ReflectorExecutionReceiptV2,
)
from openevo_chembench.source_identity_v2 import (
    verify_source_manifest,
    write_source_manifest,
)
from openevo_chembench.v2_config import (
    ExecutorPolicyV2,
    FrozenArtifactBinding,
    FrozenExperimentConfigV2,
)


_DIGEST = "a" * 64


def _config(*, arm: str) -> FrozenExperimentConfigV2:
    enabled = arm == "evolved"
    return FrozenExperimentConfigV2(
        arm=arm,
        scope="pilot500",
        run_name=f"{arm}_pilot500",
        output_directory=f"results/v2/pilot500/{arm}",
        dataset_root="data/chembench4k",
        dataset_manifest="data/chembench4k/manifest.json",
        task_manifest="manifests/pilot500_public.jsonl",
        private_task_manifest="private_manifests/pilot500_private.jsonl",
        receipt_path="manifests/benchmark_execution_receipt_v2.json",
        pilot_protocol_hash="b" * 64,
        codex_cli_version="0.144.6",
        prompt_renderer_id="chembench4k_official_five_shot_v2",
        parser_id="official_first_capital_parser_v2",
        evaluator_id="chembench4k_accuracy_v2",
        artifact=FrozenArtifactBinding(
            enabled=enabled,
            frozen_artifact_id="core-text-memory-v2" if enabled else None,
            frozen_artifact_sha256="c" * 64 if enabled else None,
            context_resolution_digest="d" * 64 if enabled else None,
            resolved_memory_sha256="e" * 64 if enabled else None,
        ),
        executor=ExecutorPolicyV2(
            backend="local_codex_cli",
            harness="codex_cli",
            timeout_seconds=600,
            concurrency=1,
            infrastructure_retries_before_completion=0,
            tools_enabled=False,
            mcp_enabled=False,
            web_enabled=False,
            network_enabled=False,
            subagents_enabled=False,
        ),
    )


def _inputs(tmp_path: Path) -> BenchmarkReceiptInputsV2:
    package = tmp_path / "benchmarks" / "chembench"
    package.mkdir(parents=True)
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    (dataset / "chembench4k_dataset_manifest_v2.json").write_text(
        "{}\n",
        encoding="utf-8",
    )
    return BenchmarkReceiptInputsV2(
        workspace_root=tmp_path,
        repository_root=tmp_path,
        package_root=package,
        dataset_root=dataset,
        source_manifest_path=package / "chembench_source_manifest_v2.json",
        pilot_public_manifest_path=tmp_path / "pilot_public.jsonl",
        pilot_private_manifest_path=tmp_path / "pilot_private.jsonl",
        pilot_summary_path=tmp_path / "pilot_summary.json",
        baseline_config_path=tmp_path / "baseline.yaml",
        evolved_config_path=tmp_path / "evolved.yaml",
        framework_lock_path=tmp_path / "framework_lock.json",
        core_database_path=tmp_path / "core.sqlite",
        core_artifact_root=tmp_path / "artifacts",
        frozen_record_path=tmp_path / "missing_frozen_record.json",
        reflector_private_audit_root=tmp_path / "reflector_private_events",
    )


def _receipt(
    *,
    findings: tuple[ReceiptFindingV2, ...] = (),
    created_at: str = "2026-07-24T00:00:00+00:00",
) -> BenchmarkExecutionReceiptV2:
    return BenchmarkExecutionReceiptV2(
        fields={"protocol_id": "chembench4k_frozen_generalization_v2"},
        finding_codes=findings,
        created_at_utc=created_at,
    )


def test_inputs_have_no_caller_verification_booleans() -> None:
    names = {field.name for field in dataclass_fields(BenchmarkReceiptInputsV2)}
    assert not any(
        name in names
        for name in (
            "dataset_verified",
            "method_registered",
            "artifact_verified",
            "security_verified",
            "paid_execution_allowed",
        )
    )
    with pytest.raises(TypeError):
        BenchmarkReceiptInputsV2(  # type: ignore[call-arg]
            **{name: Path(".") for name in names},
            dataset_verified=True,
        )


def test_product_disclaimer_is_fixed_and_reserved_fields_cannot_spoof() -> None:
    receipt = _receipt()
    payload = receipt.to_payload()
    assert payload["evidence_class"] == DISCLAIMER
    assert payload["product_release_disclaimer"] == PRODUCT_DISCLAIMER
    assert payload["paid_execution_allowed"] is True
    assert len(payload["evidence_digest"]) == 64

    with pytest.raises(ValueError):
        BenchmarkExecutionReceiptV2(
            fields={"paid_execution_allowed": True},
            finding_codes=(ReceiptFindingV2.FROZEN_ARTIFACT_MISSING,),
            created_at_utc="now",
        )


def test_receipt_fields_are_immutable_after_construction() -> None:
    source = {"protocol_id": "original"}
    receipt = BenchmarkExecutionReceiptV2(
        fields=source,
        finding_codes=(),
        created_at_utc="now",
    )
    source["protocol_id"] = "tampered"
    assert receipt.fields["protocol_id"] == "original"
    with pytest.raises(TypeError):
        receipt.fields["protocol_id"] = "tampered"  # type: ignore[index]


def test_receipt_does_not_use_its_own_sha256_as_verified_input() -> None:
    input_names = {field.name for field in dataclass_fields(BenchmarkReceiptInputsV2)}
    receipt = _receipt()
    payload = receipt.to_payload()

    assert "receipt_path" not in input_names
    assert "receipt_sha256" not in input_names
    assert "receipt_sha256" not in receipt.fields
    assert "receipt_sha256" not in payload["directly_verified_fields"]
    assert "receipt_sha256" not in payload


def test_public_authorization_constructor_is_not_forgeable() -> None:
    assert not hasattr(receipt_module, "_AUTHORIZATION_ISSUER")
    with pytest.raises(TypeError):
        BenchmarkAuthorizationV2(
            receipt_sha256=_DIGEST,
            evidence_digest="b" * 64,
        )
    with pytest.raises(TypeError):
        BenchmarkAuthorizationV2(
            receipt_sha256=_DIGEST,
            evidence_digest="b" * 64,
            _issuer_token=object(),
        )


def test_missing_frozen_artifact_has_exact_closed_blocker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _inputs(tmp_path)
    baseline = _config(arm="baseline")
    evolved = _config(arm="evolved")
    fake_loader = SimpleNamespace(manifest=SimpleNamespace(combined_sha256="1" * 64))
    fake_pilot = SimpleNamespace(
        public_sha256="2" * 64,
        summary_sha256="3" * 64,
        ordered_uid_hash="4" * 64,
    )

    monkeypatch.setattr(
        receipt_module,
        "ChemBench4KDatasetLoader",
        lambda *, snapshot_root: fake_loader,
    )
    monkeypatch.setattr(
        receipt_module,
        "verify_source_manifest",
        lambda package_root, manifest_path: "5" * 64,
    )
    monkeypatch.setattr(
        receipt_module,
        "_git",
        lambda repository_root, *arguments: (
            "6" * 40 if arguments[:2] == ("rev-parse", "HEAD") else ""
        ),
    )
    monkeypatch.setattr(
        receipt_module,
        "_core_source_manifest_digest",
        lambda repository_root: "7" * 64,
    )
    monkeypatch.setattr(
        receipt_module,
        "_source_is_committed_or_accepted",
        lambda receipt_inputs, source_digest: (True, False),
    )
    monkeypatch.setattr(
        receipt_module,
        "load_frozen_config_v2",
        lambda path: baseline if path == inputs.baseline_config_path else evolved,
    )
    monkeypatch.setattr(
        receipt_module,
        "_module_sha256",
        lambda package_root, filename: "8" * 64,
    )
    monkeypatch.setattr(
        receipt_module,
        "verify_task_manifests",
        lambda loader, **kwargs: fake_pilot,
    )
    monkeypatch.setattr(
        receipt_module,
        "_actual_codex_version",
        lambda repository_root: "0.144.6",
    )

    receipt = recompute_benchmark_receipt_v2(inputs)

    assert receipt.finding_codes == (ReceiptFindingV2.FROZEN_ARTIFACT_MISSING,)
    assert receipt.paid_execution_allowed is False
    assert receipt.fields["core_artifact_id"] is None
    assert receipt.fields["resolved_memory_sha256"] is None
    assert receipt.fields["openevo_core_git_commit"] == "6" * 40


def test_receipt_gate_recomputes_private_reflector_event_binding(tmp_path: Path) -> None:
    audit_root = tmp_path / "private_reflector_events"
    invocation = "a" * 32
    directory = audit_root / invocation
    directory.mkdir(parents=True)
    events = directory / "events.jsonl"
    events.write_text('{"type":"turn.completed"}\n', encoding="utf-8")
    events.chmod(0o600)
    event_digest = hashlib.sha256(events.read_bytes()).hexdigest()
    receipt = ReflectorExecutionReceiptV2(
        invocation_id=invocation,
        status=ReflectorBoundaryStatusV2.COMPLETED,
        mechanism="bubblewrap",
        wrapper_invoked=True,
        real_codex_sha256="1" * 64,
        dev_artifact_sha256="2" * 64,
        ordered_records_sha256="3" * 64,
        record_count=45,
        event_stream_sha256=event_digest,
        event_counts=(),
        private_event_reference=f"{invocation}/events.jsonl",
        last_message_sha256="4" * 64,
        cleanup_complete=True,
        retry_allowed=False,
        resume_allowed=False,
        replacement_completion_allowed=False,
    )
    receipt_path = directory / "receipt.json"
    receipt_path.write_text(
        json.dumps(receipt.to_payload(), sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    receipt_path.chmod(0o600)

    loaded = receipt_module._load_bound_reflector_receipt(
        audit_root,
        expected_digest=receipt.digest,
    )
    assert loaded == receipt

    events.write_text('{"type":"item.started"}\n', encoding="utf-8")
    with pytest.raises(RuntimeError, match="unavailable"):
        receipt_module._load_bound_reflector_receipt(
            audit_root,
            expected_digest=receipt.digest,
        )


class _ReadOnceReceiptPath:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.calls = 0

    def read_bytes(self) -> bytes:
        self.calls += 1
        if self.calls > 1:
            raise AssertionError("receipt must be hashed from the verified read")
        return self.content


def test_authorization_recomputes_and_hashes_the_verified_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _inputs(tmp_path)
    stored_receipt = _receipt(created_at="stored-time")
    current_receipt = _receipt(created_at="recomputed-time")
    receipt_path = _ReadOnceReceiptPath(stored_receipt.canonical_bytes())
    calls = 0

    def recompute(_inputs_value: BenchmarkReceiptInputsV2):
        nonlocal calls
        calls += 1
        return current_receipt

    monkeypatch.setattr(
        receipt_module,
        "recompute_benchmark_receipt_v2",
        recompute,
    )

    authorization = verify_receipt_and_issue_authorization_v2(
        inputs,
        receipt_path,  # type: ignore[arg-type]
    )

    assert calls == 1
    assert receipt_path.calls == 1
    assert authorization.receipt_sha256 == hashlib.sha256(receipt_path.content).hexdigest()
    assert authorization.evidence_digest == current_receipt.evidence_digest


def test_receipt_create_delete_and_rebuild_do_not_change_source_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _inputs(tmp_path)
    source = inputs.package_root / "src" / "openevo_chembench" / "module.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    source_manifest, source_sha256 = write_source_manifest(inputs.package_root)
    source_bytes = source_manifest.read_bytes()
    receipt_path = inputs.package_root / "manifests" / "v2" / "benchmark_execution_receipt_v2.json"
    monkeypatch.setattr(
        receipt_module,
        "recompute_benchmark_receipt_v2",
        lambda _inputs_value: _receipt(),
    )

    write_benchmark_receipt_v2(inputs, receipt_path)
    assert verify_source_manifest(inputs.package_root, source_manifest) == source_sha256
    assert source_manifest.read_bytes() == source_bytes

    receipt_path.unlink()
    write_benchmark_receipt_v2(inputs, receipt_path)
    assert verify_source_manifest(inputs.package_root, source_manifest) == source_sha256
    assert source_manifest.read_bytes() == source_bytes


def test_frozen_receipt_reverification_is_byte_preserving(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _inputs(tmp_path)
    first = _receipt(created_at="2026-07-24T00:00:00+00:00")
    current = _receipt(created_at="2026-07-24T00:01:00+00:00")
    receipts = iter((first, current))
    monkeypatch.setattr(
        receipt_module,
        "recompute_benchmark_receipt_v2",
        lambda _inputs_value: next(receipts),
    )
    receipt_path = tmp_path / "benchmark_execution_receipt_v2.json"

    write_benchmark_receipt_v2(inputs, receipt_path)
    frozen_bytes = receipt_path.read_bytes()
    authorization = verify_receipt_and_issue_authorization_v2(inputs, receipt_path)

    assert receipt_path.read_bytes() == frozen_bytes
    assert first.evidence_digest == current.evidence_digest
    assert authorization.receipt_sha256 == hashlib.sha256(frozen_bytes).hexdigest()
    assert authorization.evidence_digest == current.evidence_digest


def test_blocked_or_spoofed_receipt_never_issues_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _inputs(tmp_path)
    blocked = _receipt(findings=(ReceiptFindingV2.FROZEN_ARTIFACT_MISSING,))
    monkeypatch.setattr(
        receipt_module,
        "recompute_benchmark_receipt_v2",
        lambda _inputs_value: blocked,
    )
    exact_path = _ReadOnceReceiptPath(blocked.canonical_bytes())
    with pytest.raises(RuntimeError, match="blocked"):
        verify_receipt_and_issue_authorization_v2(
            inputs,
            exact_path,  # type: ignore[arg-type]
        )

    spoofed = blocked.to_payload()
    spoofed["paid_execution_allowed"] = True
    spoofed["finding_codes"] = []
    spoofed_path = _ReadOnceReceiptPath((json.dumps(spoofed, sort_keys=True) + "\n").encode())
    with pytest.raises(RuntimeError, match="no longer matches"):
        verify_receipt_and_issue_authorization_v2(
            inputs,
            spoofed_path,  # type: ignore[arg-type]
        )

    non_mapping_path = _ReadOnceReceiptPath(b"[]\n")
    with pytest.raises(RuntimeError, match="unavailable or invalid"):
        verify_receipt_and_issue_authorization_v2(
            inputs,
            non_mapping_path,  # type: ignore[arg-type]
        )
