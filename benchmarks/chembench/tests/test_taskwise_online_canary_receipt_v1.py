from __future__ import annotations

import hashlib
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

import pytest

import openevo_chembench.taskwise_online_canary_receipt_v1 as receipt_module
from openevo_chembench.taskwise_online_canary_receipt_v1 import (
    ONLINE_CLASSIFICATION,
    OnlineCanaryFindingV1,
    OnlineCanaryMechanismReceiptV1,
    OnlineCanaryPilotAuthorizationV1,
    OnlineCanaryReceiptInputsV1,
    current_online_canary_generation_id_v1,
    default_online_canary_receipt_path_v1,
    verify_online_canary_receipt_v1,
    write_online_canary_receipt_v1,
)
from openevo_chembench.taskwise_sampling_v1 import PILOT500_STREAM_SCOPES


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_GENERATION_ID = f"online_canary_{'a' * 64}"


def _inputs(tmp_path: Path) -> OnlineCanaryReceiptInputsV1:
    return OnlineCanaryReceiptInputsV1(
        repository_root=tmp_path / "repository",
        package_root=tmp_path / "package",
        dataset_root=tmp_path / "dataset",
        source_manifest_path=tmp_path / "source.json",
        framework_lock_path=tmp_path / "framework-lock.json",
        online_config_path=tmp_path / "online.yaml",
    )


def _positive_receipt(*, marker: str = "a") -> OnlineCanaryMechanismReceiptV1:
    return OnlineCanaryMechanismReceiptV1(
        fields={
            "source_commit": marker * 40,
            "full4009_authorization": "MISSING",
            "full4009_execution_allowed": False,
            "full4009_status": "NOT_STARTED_WAITING_FOR_USER",
            "online_canary_generation_id": (f"online_canary_{marker * 64}"),
            "online_pilot_binding_sha256": marker * 64,
            "run_status": "COMPLETED",
            "task_session_count": 27,
            "online_update_count": 18,
            "online_core_job_count": 18,
            "online_core_artifact_count": 18,
            "online_context_resolution_count": 18,
            "security_violation_count": 0,
            "context_binding_violation_count": 0,
            "artifact_chain_violation_count": 0,
            "task_cleanup_residual_root_count": 0,
            "reflector_cleanup_residual_root_count": 0,
            "cross_task_memory_carry_verified": True,
        },
        finding_codes=(),
        created_at_utc="2026-07-26T00:00:00+00:00",
    )


def test_online_receipt_inputs_accept_paths_not_caller_booleans(tmp_path: Path) -> None:
    names = {item.name for item in fields(OnlineCanaryReceiptInputsV1)}

    assert names == {
        "repository_root",
        "package_root",
        "dataset_root",
        "source_manifest_path",
        "framework_lock_path",
        "online_config_path",
    }
    assert "control_config_path" not in names
    assert not {
        "passed",
        "verified",
        "online_only_pilot_allowed",
        "security_verified",
    }.intersection(names)
    with pytest.raises(TypeError, match="pathlib.Path"):
        OnlineCanaryReceiptInputsV1(
            repository_root="repository",  # type: ignore[arg-type]
            package_root=tmp_path,
            dataset_root=tmp_path,
            source_manifest_path=tmp_path,
            framework_lock_path=tmp_path,
            online_config_path=tmp_path,
        )


def test_online_authorization_is_not_publicly_forgeable() -> None:
    with pytest.raises(TypeError, match="issued only"):
        OnlineCanaryPilotAuthorizationV1(
            receipt_sha256="a" * 64,
            evidence_digest="b" * 64,
            source_commit="c" * 40,
            online_canary_generation_id=_GENERATION_ID,
            online_pilot_binding_sha256="d" * 64,
        )


def test_online_receipt_labels_are_complete_and_have_no_control_arm() -> None:
    assert set(ONLINE_CLASSIFICATION) == {
        "ONLINE_TASKWISE_EVOLUTION",
        "TEST_TIME_ADAPTATION",
        "ONLINE_ONLY_PILOT500",
        "UNPAIRED_ONLINE_ONLY_NOT_FOR_PAIRED_INFERENCE",
        "ONLINE_CANARY_MECHANISM_AND_SECURITY_ONLY",
        "NO_CONTROL_ARM",
        "NOT_A_PERFORMANCE_COMPARISON",
        "NON_STANDARD_CHEMBENCH4K_PROTOCOL",
        "NOT_A_STANDARD_LEADERBOARD_SCORE",
        "DESCRIPTIVE_ONLINE_ONLY_RESULT",
        "NO_CAUSAL_CONTROL_COMPARISON",
        "NO_PAIRED_PERFORMANCE_CLAIM",
    }
    payload = _positive_receipt().to_payload()
    assert payload["online_only_pilot_allowed"] is True
    assert payload["full4009_execution_allowed"] is False
    assert payload["full4009_authorization"] == "MISSING"
    assert payload["full4009_status"] == "NOT_STARTED_WAITING_FOR_USER"
    assert "control_config_sha256" not in payload
    assert "control_run_id" not in payload
    assert "control_completion_count" not in payload


def test_online_receipt_exact_recomputation_issues_capability(
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
        "recompute_online_canary_receipt_v1",
        lambda actual: stored if actual is inputs else pytest.fail("wrong inputs"),
    )

    authorization = verify_online_canary_receipt_v1(inputs, path)

    assert authorization.receipt_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert authorization.evidence_digest == stored.evidence_digest
    assert authorization.source_commit == "a" * 40
    assert authorization.online_canary_generation_id == _GENERATION_ID
    assert authorization.online_pilot_binding_sha256 == "a" * 64


def test_online_receipt_drift_and_blocked_evidence_fail_closed(
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
        "recompute_online_canary_receipt_v1",
        lambda _inputs: _positive_receipt(marker="b"),
    )
    with pytest.raises(RuntimeError, match="no longer matches"):
        verify_online_canary_receipt_v1(inputs, path)

    blocked = OnlineCanaryMechanismReceiptV1(
        fields={"source_commit": "a" * 40},
        finding_codes=(OnlineCanaryFindingV1.ONLINE_CANARY_RUN_INVALID,),
        created_at_utc="2026-07-26T00:00:00+00:00",
    )
    monkeypatch.setattr(
        receipt_module,
        "recompute_online_canary_receipt_v1",
        lambda _inputs: blocked,
    )
    blocked_path = tmp_path / "blocked.json"
    with pytest.raises(RuntimeError, match="findings block"):
        write_online_canary_receipt_v1(inputs, blocked_path)
    assert not blocked_path.exists()


def test_generation_id_reads_only_online_canary_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _inputs(tmp_path)
    loaded: list[Path] = []
    config = SimpleNamespace(
        config_sha256=lambda: "c" * 64,
        run_name="online_fix20",
        model="gpt-5.5",
        reasoning_effort="medium",
        codex_cli_version="0.144.6",
    )
    loader = SimpleNamespace(
        manifest=SimpleNamespace(combined_sha256="d" * 64),
    )
    manifest = SimpleNamespace(
        public_sha256="e" * 64,
        ordered_uid_sha256="f" * 64,
    )
    monkeypatch.setattr(receipt_module, "_git", lambda *_args: "1" * 40)
    monkeypatch.setattr(
        receipt_module,
        "verify_source_manifest",
        lambda *_args: "2" * 64,
    )
    monkeypatch.setattr(
        receipt_module,
        "load_taskwise_config_v1",
        lambda path: loaded.append(path) or config,
    )
    monkeypatch.setattr(receipt_module, "ChemBench4KDatasetLoader", lambda **_kwargs: loader)
    monkeypatch.setattr(
        receipt_module, "_verify_online_manifest", lambda *_args, **_kwargs: manifest
    )

    generation = current_online_canary_generation_id_v1(inputs)

    assert generation.startswith("online_canary_")
    assert loaded == [inputs.online_config_path]
    assert "control" not in loaded[0].name.casefold()


def test_online_pilot_binding_loads_only_ten_online_stream_configs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = tmp_path / "package"
    summary_path = package / "manifests" / "taskwise_online_v1"
    summary_path.mkdir(parents=True)
    summary = {
        "stream_count": 10,
        "tasks_per_stream": 50,
        "total_item_count": 500,
        "reset_memory_between_streams": True,
        "global_ordered_uid_sha256": "1" * 64,
    }
    summary_bytes = receipt_module._canonical_bytes(summary)
    (summary_path / "online_pilot500_summary.json").write_bytes(summary_bytes)
    inputs = OnlineCanaryReceiptInputsV1(
        repository_root=tmp_path / "repository",
        package_root=package,
        dataset_root=tmp_path / "dataset",
        source_manifest_path=tmp_path / "source.json",
        framework_lock_path=tmp_path / "framework.json",
        online_config_path=tmp_path / "online.yaml",
    )
    loaded: list[Path] = []

    def load_config(path: Path) -> SimpleNamespace:
        loaded.append(path)
        scope = path.name.removeprefix("online_").removesuffix("_taskwise_online_v1.yaml")
        return SimpleNamespace(
            arm="online",
            scope=scope,
            config_sha256=lambda scope=scope: hashlib.sha256(scope.encode()).hexdigest(),
        )

    manifests = {
        scope: SimpleNamespace(
            public_sha256=hashlib.sha256(f"public:{scope}".encode()).hexdigest(),
            private_sha256=hashlib.sha256(f"private:{scope}".encode()).hexdigest(),
            ordered_uid_sha256=hashlib.sha256(f"uids:{scope}".encode()).hexdigest(),
        )
        for scope in PILOT500_STREAM_SCOPES
    }
    monkeypatch.setattr(receipt_module, "load_taskwise_config_v1", load_config)
    monkeypatch.setattr(
        receipt_module,
        "_verify_online_manifest",
        lambda _inputs, config, _loader, *, scope: (
            manifests[scope] if config.scope == scope else pytest.fail("scope mismatch")
        ),
    )
    monkeypatch.setattr(
        receipt_module,
        "pilot500_stream_suite_summary_bytes",
        lambda _loader, *, stream_manifests: (
            summary_bytes
            if tuple(stream_manifests) == tuple(manifests.values())
            else pytest.fail("manifest order mismatch")
        ),
    )

    binding = receipt_module._recompute_online_pilot_binding(
        inputs,
        object(),
        _GENERATION_ID,
    )

    assert len(loaded) == 10
    assert all(path.name.startswith("online_") for path in loaded)
    assert all("control" not in path.name for path in loaded)
    assert binding["online_pilot_ordered_uid_sha256"] == "1" * 64


def test_default_receipt_path_is_private_state_not_results() -> None:
    path = default_online_canary_receipt_path_v1(PACKAGE_ROOT, _GENERATION_ID)
    assert path.relative_to(PACKAGE_ROOT).as_posix() == (
        "state/taskwise_online_v1/online_canary_receipts/"
        f"{_GENERATION_ID}/online_canary_mechanism_receipt_v1.json"
    )
    assert "results" not in path.parts
    assert "private_manifests" not in path.parts


def test_online_scripts_use_independent_receipt_and_fix20_namespace() -> None:
    canary = (PACKAGE_ROOT / "scripts" / "run_online_canary9_taskwise_online_v1.sh").read_text(
        encoding="utf-8"
    )
    pilot = (
        PACKAGE_ROOT / "scripts" / "run_online_only_pilot500_streams_taskwise_online_v1.sh"
    ).read_text(encoding="utf-8")
    config = (PACKAGE_ROOT / "configs" / "online_canary9_taskwise_online_v1.yaml").read_text(
        encoding="utf-8"
    )

    assert "taskwise_online_canary_receipt_v1" in canary
    assert "run_taskwise_cli freeze" in canary
    assert "run_taskwise_cli verify" in canary
    assert "taskwise_online_canary_receipt_v1" in pilot
    assert "run_taskwise_cli verify-canary-receipt" not in pilot
    assert "control_canary" not in pilot
    assert "control_pilot" not in pilot
    assert "online_fix20" in config
    assert "online_fix19" not in config


def test_online_receipt_cli_rejects_caller_pass_boolean() -> None:
    with pytest.raises(SystemExit):
        receipt_module.main(["verify", "--passed"])
