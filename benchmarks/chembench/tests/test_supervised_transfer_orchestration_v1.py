from __future__ import annotations

import hashlib
import json
import subprocess
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self, cast

import pytest

from openevo_chembench.chembench4k_models import CHEMBENCH4K_CATEGORIES
from openevo_chembench.local_codex_executor import (
    LocalCodexExecutionError,
    LocalCodexExecutionErrorCode,
)
from openevo_chembench.supervised_transfer_v1.config import (
    load_supervised_transfer_config_v1,
)
from openevo_chembench.supervised_transfer_v1.experiment import (
    ExperimentInputsV1,
    SupervisedExperimentError,
    SupervisedTransferExperimentV1,
)
from openevo_chembench.supervised_transfer_v1.managed_codex import (
    OpenEvoManagedCodexIdentityV1,
)
from openevo_chembench.supervised_transfer_v1.pause import (
    AdministrativePauseError,
    _relevant_process_snapshot,
    record_administrative_pause_v1,
)
from openevo_chembench.supervised_transfer_v1.preflight import (
    _aggregate_checkpoint_zero,
    _empty_checkpoint_targets,
    _valid_multitarget_core_row,
)
from openevo_chembench.supervised_transfer_v1.reporting import _probe_rows
from openevo_chembench.supervised_transfer_v1.test_ledger import (
    TEST_LEDGER_FILENAME,
    claim_test_manifest_use_v1,
    complete_test_manifest_use_v1,
    invalidate_test_manifest_after_source_bug_v1,
)
from openevo_chembench.supervised_transfer_v1.test_ledger import (
    TestManifestLedgerError as LedgerError,
)
from openevo_chembench.taskwise_config_v1 import load_taskwise_config_v1

WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = (
    WORKSPACE_ROOT
    / "benchmarks/chembench/configs/chembench_supervised_transfer_v1.yaml"
)


def _experiment_inputs(tmp_path: Path) -> ExperimentInputsV1:
    managed_codex = OpenEvoManagedCodexIdentityV1(
        root=tmp_path / "state/chembench_supervised_transfer_v1/managed_codex",
        executable=Path("/bin/true"),
        executable_sha256="3" * 64,
        launcher_sha256="4" * 64,
        package_json_sha256="5" * 64,
        platform_package_json_sha256="6" * 64,
        codex_cli_version="codex-cli 0.1.0",
        npm_package="@openai/codex@0.1.0",
        openevo_distribution_version="0.1.8",
        receipt_sha256="7" * 64,
    )
    return ExperimentInputsV1(
        repository_root=tmp_path,
        config=load_supervised_transfer_config_v1(CONFIG_PATH),
        loader=cast(Any, None),
        train=(),
        probe=(),
        test=(),
        manifest_root=tmp_path,
        split_receipt_sha256="1" * 64,
        source_commit="2" * 40,
        codex_cli_version="codex-cli 0.1.0",
        managed_codex=managed_codex,
        test_manifest="test_primary",
    )


def _ledger_inputs(
    tmp_path: Path,
    *,
    source_commit: str,
    manifest: str,
) -> Any:
    manifests = tmp_path / "manifests"
    manifests.mkdir(exist_ok=True)
    path = manifests / f"{manifest}_private_manifest.jsonl"
    if not path.exists():
        path.write_text('{"uid":"' + ("a" if manifest == "test_primary" else "b") + '"}\n')
        path.chmod(0o600)
    return SimpleNamespace(
        repository_root=tmp_path,
        config=SimpleNamespace(
            result_root="results/root",
            state_root="state/chembench_supervised_transfer_v1",
            protocol_id="chembench_supervised_transfer_v1",
        ),
        test_manifest=manifest,
        manifest_root=manifests,
        test=(SimpleNamespace(uid="a" if manifest == "test_primary" else "b"),),
        source_commit=source_commit,
        split_receipt_sha256="3" * 64,
    )


def test_run_mode_and_stage_machine_fail_closed_before_paid_work(tmp_path: Path) -> None:
    preflight = SupervisedTransferExperimentV1(
        inputs=_experiment_inputs(tmp_path),
        run_id="preflight-test-0001",
        run_mode="preflight",
    )
    with pytest.raises(SupervisedExperimentError, match="RUN_MODE_STAGE_INVALID"):
        preflight._set_stage("CONTROL_TRAIN")

    formal = SupervisedTransferExperimentV1(
        inputs=_experiment_inputs(tmp_path),
        run_id="formal-test-0001",
        run_mode="formal",
        preflight_run_id="preflight-source-0001",
    )
    with pytest.raises(SupervisedExperimentError, match="RUN_MODE_STAGE_INVALID"):
        formal._set_stage("SUPERVISED_UPDATE_SMOKE")
    formal._set_stage("CONTROL_TRAIN")
    with pytest.raises(SupervisedExperimentError, match="CONTROL_TRAIN_NOT_COMPLETE"):
        formal._set_stage("ONLINE_CORE_INITIALIZATION")
    formal._state["task_sessions"] = 1_350
    formal._set_stage("ONLINE_CORE_INITIALIZATION")
    with pytest.raises(SupervisedExperimentError, match="STAGE_TRANSITION_INVALID"):
        formal._set_stage("FINAL_TEST")


def test_paid_run_rejects_uncommitted_benchmark_source(tmp_path: Path) -> None:
    subprocess.run(("git", "init", "-q"), cwd=tmp_path, check=True)
    source = tmp_path / "benchmarks/chembench/src/uncommitted.py"
    source.parent.mkdir(parents=True)
    source.write_text("UNCOMMITTED = True\n", encoding="utf-8")

    with pytest.raises(SupervisedExperimentError, match="BENCHMARK_SOURCE_NOT_COMMITTED"):
        SupervisedTransferExperimentV1(
            inputs=_experiment_inputs(tmp_path),
            run_id="dirty-source-test-0001",
            run_mode="preflight",
        )
    assert not (tmp_path / "results/chembench_supervised_transfer_v1").exists()


def test_task_and_reflector_paths_use_only_explicit_openevo_managed_codex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _experiment_inputs(tmp_path)
    experiment = SupervisedTransferExperimentV1(
        inputs=inputs,
        run_id="managed-codex-test-0001",
        run_mode="preflight",
    )
    executor_paths: list[Path] = []

    class FakeExecutor:
        def __init__(self, **arguments: object) -> None:
            executor_paths.append(cast(Path, arguments["codex_executable"]))

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(
        "openevo_chembench.supervised_transfer_v1.experiment.LocalCodexCLIExecutor",
        FakeExecutor,
    )
    taskwise_config = load_taskwise_config_v1(
        WORKSPACE_ROOT
        / "benchmarks/chembench/configs/control_canary9_taskwise_online_v1.yaml"
    )
    monkeypatch.setattr(
        "openevo_chembench.supervised_transfer_v1.experiment.load_taskwise_config_v1",
        lambda _path: taskwise_config,
    )
    with ExitStack() as stack:
        experiment._open_executors(stack)
    assert executor_paths == [inputs.managed_codex.executable] * 2

    bridge_arguments: dict[str, object] = {}

    def fake_bridge(**arguments: object) -> object:
        bridge_arguments.update(arguments)
        return object()

    monkeypatch.setattr(
        "openevo_chembench.supervised_transfer_v1.experiment."
        "build_supervised_core_bridge_at_roots_v1",
        fake_bridge,
    )
    experiment._new_ephemeral_bridge("scope", CHEMBENCH4K_CATEGORIES[0])
    assert bridge_arguments["codex_executable"] == inputs.managed_codex.executable


def test_probe_smoke_admits_only_probe_smoke_arms(tmp_path: Path) -> None:
    inputs = _experiment_inputs(tmp_path)
    probe_task = SimpleNamespace(uid="probe-task")
    object.__setattr__(inputs, "probe", (probe_task,))
    experiment = SupervisedTransferExperimentV1(
        inputs=inputs,
        run_id="probe-smoke-test-0001",
        run_mode="preflight",
    )
    experiment._state["stage"] = "PROBE_SMOKE"

    for arm in ("control_probe_smoke", "online_probe_smoke"):
        experiment._require_session_admission(
            task=cast(Any, probe_task),
            context=None,
            logical_arm=arm,
            task_ordinal=0,
            round_index=0,
            stage="PROBE_SMOKE",
        )
    with pytest.raises(SupervisedExperimentError, match="SESSION_ARM_STAGE_INVALID"):
        experiment._require_session_admission(
            task=cast(Any, probe_task),
            context=None,
            logical_arm="control_probe",
            task_ordinal=0,
            round_index=0,
            stage="PROBE_SMOKE",
        )


def test_preflight_requires_complete_multitarget_core_evidence() -> None:
    row = {
        "method_id": "text_memory_expel_reflector",
        "auxiliary_targets": [
            {
                "target_id": target,
                "method_id": target,
                "job_id": f"job-{target}",
                "artifact_id": f"artifact-{target}",
                "inspection": {
                    "target_id": target,
                    "finding_codes": [],
                },
            }
            for target in ("skill_bundle", "agent_system")
        ],
    }
    assert _valid_multitarget_core_row(row)
    row["auxiliary_targets"] = row["auxiliary_targets"][:1]
    assert not _valid_multitarget_core_row(row)
    assert _empty_checkpoint_targets(
        {
            "text_memory": None,
            "skill_bundle": None,
            "agent_system": None,
        }
    )
    assert not _empty_checkpoint_targets({"text_memory": None})


def test_formal_run_calls_control_before_bridges_or_online(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    experiment = SupervisedTransferExperimentV1(
        inputs=_experiment_inputs(tmp_path),
        run_id="formal-order-test-0001",
        run_mode="formal",
        preflight_run_id="preflight-source-0001",
    )
    actions: list[str] = []
    authority = {"checkpoint_zero_aggregate_rows": []}
    monkeypatch.setattr(
        "openevo_chembench.supervised_transfer_v1.experiment."
        "verify_preflight_authority_v1",
        lambda **_kwargs: (authority, "4" * 64),
    )
    monkeypatch.setattr(
        experiment,
        "_open_executors",
        lambda _stack: (object(), object()),
    )

    def control_train(_executor: object) -> None:
        actions.append("CONTROL_TRAIN")
        experiment._set_stage("CONTROL_TRAIN")
        experiment._state["task_sessions"] = 1_350

    def open_bridges(_stack: object) -> None:
        actions.append("ONLINE_CORE_INITIALIZATION")
        experiment._set_stage("ONLINE_CORE_INITIALIZATION")

    def freeze_checkpoint(checkpoint: int) -> object:
        actions.append(f"FREEZE_{checkpoint}")
        return object()

    def online_train(_control: object, _online: object) -> None:
        actions.append("ONLINE_TRAIN")
        experiment._set_stage("ONLINE_TRAIN")
        experiment._state["task_sessions"] = 3_420
        experiment._state["reflector_completions"] = 900
        experiment._state["core_jobs"] = 2_700
        experiment._state["core_artifacts"] = 2_700
        experiment._set_stage("PROBE_CHECKPOINT_50")
        experiment._state["task_sessions"] = 3_600

    def freeze_final() -> object:
        actions.append("FREEZE_FINAL_CONTEXT")
        experiment._set_stage("FREEZE_FINAL_CONTEXT")
        return object()

    def final_test(_frozen: object, _control: object, _online: object) -> None:
        actions.append("FINAL_TEST")
        experiment._set_stage("FINAL_TEST")
        experiment._state["task_sessions"] = 4_500
        experiment._state["core_jobs"] = 2_700
        experiment._state["core_artifacts"] = 2_700

    monkeypatch.setattr(experiment, "_run_control_train", control_train)
    monkeypatch.setattr(experiment, "_open_formal_bridges", open_bridges)
    monkeypatch.setattr(experiment, "_freeze_checkpoint", freeze_checkpoint)
    monkeypatch.setattr(experiment, "_run_online_train_with_probes", online_train)
    monkeypatch.setattr(experiment, "_freeze_final_context", freeze_final)
    monkeypatch.setattr(experiment, "_run_final_test", final_test)
    monkeypatch.setattr(
        "openevo_chembench.supervised_transfer_v1.reporting.build_all_reports_v1",
        lambda **_kwargs: {"final_report_sha256": "5" * 64},
    )

    experiment.run_formal()

    assert actions[:3] == [
        "CONTROL_TRAIN",
        "ONLINE_CORE_INITIALIZATION",
        "FREEZE_0",
    ]
    assert actions.index("CONTROL_TRAIN") < actions.index("ONLINE_TRAIN")
    assert experiment._state["status"] == "COMPLETED"


def test_test_retry_is_limited_to_no_completion_infrastructure_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    experiment = SupervisedTransferExperimentV1(
        inputs=_experiment_inputs(tmp_path),
        run_id="formal-retry-test-0001",
        run_mode="formal",
        preflight_run_id="preflight-source-0001",
    )
    attempts: list[int] = []
    expected = object()

    def retryable(_executor: object, **arguments: object) -> object:
        attempts.append(int(arguments["infrastructure_attempt"]))
        if len(attempts) < 3:
            raise LocalCodexExecutionError(
                LocalCodexExecutionErrorCode.CLI_FAILED,
                taskwise_failure_code="EXECUTOR_MODEL_TRANSPORT_FAILED",
                executor_stage="MODEL_TRANSPORT",
                completion_observed=False,
            )
        return expected

    monkeypatch.setattr(experiment, "_execute_session", retryable)
    assert experiment._execute_test_session(cast(Any, object())) is expected
    assert attempts == [1, 2, 3]

    attempts.clear()

    def completion_observed(_executor: object, **arguments: object) -> object:
        attempts.append(int(arguments["infrastructure_attempt"]))
        raise LocalCodexExecutionError(
            LocalCodexExecutionErrorCode.TRANSCRIPT_INVALID,
            taskwise_failure_code="EXECUTOR_OUTPUT_INVALID",
            executor_stage="COMPLETION_VALIDATION",
            completion_observed=True,
        )

    monkeypatch.setattr(experiment, "_execute_session", completion_observed)
    with pytest.raises(LocalCodexExecutionError):
        experiment._execute_test_session(cast(Any, object()))
    assert attempts == [1]


def test_checkpoint_zero_authority_is_aggregate_only_and_report_merge_is_closed() -> None:
    rows: list[dict[str, object]] = []
    for category in CHEMBENCH4K_CATEGORIES:
        for ordinal in range(10):
            uid = f"{category}-{ordinal}"
            for arm in ("control_probe", "online_probe"):
                rows.append(
                    {
                        "kind": "PRIVATE_EVALUATED",
                        "stage": "PROBE_CHECKPOINT_00",
                        "logical_arm": arm,
                        "checkpoint": 0,
                        "task_uid": uid,
                        "category": category,
                        "correct": ordinal % 2 == 0,
                        "strict_parse_status": "parsed",
                        "target": "A",
                        "raw_completion": "A",
                    }
                )
    aggregate = _aggregate_checkpoint_zero(rows)
    forbidden = {"task_uid", "target", "raw_completion", "prediction", "correct"}
    assert len(aggregate) == 10
    assert all(not (set(row) & forbidden) for row in aggregate)

    formal: list[dict[str, object]] = []
    for checkpoint in (10, 20, 30, 40, 50):
        for category in CHEMBENCH4K_CATEGORIES:
            for ordinal in range(10):
                uid = f"{category}-{ordinal}"
                for arm in ("control_probe", "online_probe"):
                    formal.append(
                        {
                            "kind": "PRIVATE_EVALUATED",
                            "logical_arm": arm,
                            "checkpoint": checkpoint,
                            "task_uid": uid,
                            "category": category,
                            "correct": True,
                        }
                    )
    merged = _probe_rows(formal, checkpoint_zero_rows=cast(list[object], aggregate))
    assert len(merged) == 60
    assert [row for row in merged if row["checkpoint"] == 0] == [
        {key: value for key, value in row.items() if key not in {
            "control_strict_parse_rate",
            "online_strict_parse_rate",
        }}
        for row in aggregate
    ]
    with pytest.raises(RuntimeError, match="duplicated checkpoint-zero"):
        _probe_rows(rows + formal, checkpoint_zero_rows=cast(list[object], aggregate))


def test_administrative_pause_receipt_is_exclusive_and_does_not_rewrite_state(
    tmp_path: Path,
) -> None:
    result = tmp_path / "results/root/runs/paused-run-0001/public"
    private = tmp_path / "state/root/runs/paused-run-0001/private"
    result.mkdir(parents=True)
    private.mkdir(parents=True)
    state = {
        "run_id": "paused-run-0001",
        "status": "INITIALIZED",
        "stage": "ONLINE_CANARY",
        "source_commit": "a" * 40,
        "split_receipt_sha256": "b" * 64,
        "task_sessions": 20,
        "reflector_completions": 14,
        "core_jobs": 14,
        "core_artifacts": 14,
    }
    state_path = result / "run_state.json"
    state_path.write_text(json.dumps(state))
    (result / "events.jsonl").write_text('{"kind":"public"}\n')
    (private / "events.jsonl").write_text('{"kind":"private"}\n')
    before = state_path.read_bytes()

    receipt, digest = record_administrative_pause_v1(
        repository_root=tmp_path,
        result_root_relative="results/root",
        state_root_relative="state/root",
        run_id="paused-run-0001",
    )

    assert receipt["administrative_status"] == "PAUSED_NONRESUMABLE"
    assert receipt["active_model_calls"] == 0
    assert len(digest) == 64
    assert state_path.read_bytes() == before
    with pytest.raises(AdministrativePauseError, match="PAUSE_RECEIPT_ALREADY_EXISTS"):
        record_administrative_pause_v1(
            repository_root=tmp_path,
            result_root_relative="results/root",
            state_root_relative="state/root",
            run_id="paused-run-0001",
        )


def test_pause_process_scan_excludes_current_audit_ancestor_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_rows = (
        "100 50 100 100 S python record-pause\n"
        "50 40 40 40 S bash ps | rg 'codex exec|reflector'\n"
        "40 1 40 40 S tool-runner codex exec literal-in-parent-command\n"
        "200 1 200 200 S codex exec actual-active-call"
    )
    monkeypatch.setattr(
        "openevo_chembench.supervised_transfer_v1.pause.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=process_rows,
        ),
    )
    monkeypatch.setattr(
        "openevo_chembench.supervised_transfer_v1.pause.os.getpid",
        lambda: 100,
    )
    monkeypatch.setattr(
        "openevo_chembench.supervised_transfer_v1.pause.os.getppid",
        lambda: 50,
    )

    snapshot = _relevant_process_snapshot(
        repository_root=Path.cwd(),
        run_id="paused-run-0001",
    )

    assert snapshot == []


def test_pause_process_scan_is_scoped_to_repository_and_includes_descendants(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = tmp_path.resolve()
    process_rows = (
        "100 50 100 100 S python record-pause\n"
        "50 40 40 40 S bash audit-parent\n"
        "40 1 40 40 S tool-runner\n"
        "200 1 200 200 S python /repo/other/run-formal --run-id other-run-0001\n"
        "201 200 201 201 S codex exec other-call\n"
        f"300 1 300 300 S python {repository}/run-formal --run-id formal-run-0001\n"
        "301 300 301 301 S codex exec current-call"
    )
    monkeypatch.setattr(
        "openevo_chembench.supervised_transfer_v1.pause.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=process_rows,
        ),
    )
    monkeypatch.setattr(
        "openevo_chembench.supervised_transfer_v1.pause.os.getpid",
        lambda: 100,
    )
    monkeypatch.setattr(
        "openevo_chembench.supervised_transfer_v1.pause.os.getppid",
        lambda: 50,
    )
    snapshot = _relevant_process_snapshot(
        repository_root=repository,
        run_id="formal-run-0001",
    )

    assert [row["pid"] for row in snapshot] == [300, 301]


def test_test_manifest_ledger_is_global_single_use_and_recovery_is_evidence_gated(
    tmp_path: Path,
) -> None:
    subprocess.run(("git", "init", "-q"), cwd=tmp_path, check=True)
    subprocess.run(("git", "config", "user.email", "test@example.invalid"), cwd=tmp_path, check=True)
    subprocess.run(("git", "config", "user.name", "Test"), cwd=tmp_path, check=True)
    source = tmp_path / "source.txt"
    source.write_text("old\n")
    subprocess.run(("git", "add", "source.txt"), cwd=tmp_path, check=True)
    subprocess.run(("git", "commit", "-qm", "old"), cwd=tmp_path, check=True)
    old_commit = subprocess.check_output(("git", "rev-parse", "HEAD"), cwd=tmp_path, text=True).strip()
    primary = _ledger_inputs(
        tmp_path,
        source_commit=old_commit,
        manifest="test_primary",
    )
    claim_test_manifest_use_v1(
        inputs=primary,
        run_id="formal-ledger-test-0001",
        frozen_receipt_sha256="4" * 64,
        claimed_at_utc="2026-01-01T00:00:00Z",
    )
    with pytest.raises(LedgerError, match="TEST_MANIFEST_ALREADY_CLAIMED"):
        claim_test_manifest_use_v1(
            inputs=primary,
            run_id="formal-ledger-test-0002",
            frozen_receipt_sha256="4" * 64,
            claimed_at_utc="2026-01-01T00:00:01Z",
        )
    recovery_old = _ledger_inputs(
        tmp_path,
        source_commit=old_commit,
        manifest="test_recovery_01",
    )
    with pytest.raises(LedgerError, match="RECOVERY_TEST_NOT_AUTHORIZED"):
        claim_test_manifest_use_v1(
            inputs=recovery_old,
            run_id="formal-ledger-test-0003",
            frozen_receipt_sha256="4" * 64,
            claimed_at_utc="2026-01-01T00:00:02Z",
        )

    public = tmp_path / "results/root/runs/formal-ledger-test-0001/public"
    public.mkdir(parents=True)
    (public / "run_state.json").write_text(
        json.dumps(
            {
                "status": "FAIL_CLOSED",
                "stage": "FINAL_TEST",
                "source_commit": old_commit,
            }
        )
    )
    (public / "test_primary_use_receipt.json").write_text(
        json.dumps(
            {
                "manifest": "test_primary",
                "reason": "PRIMARY_PREREGISTERED_TEST",
            }
        )
    )
    source.write_text("new\n")
    subprocess.run(("git", "add", "source.txt"), cwd=tmp_path, check=True)
    subprocess.run(("git", "commit", "-qm", "fix implementation bug"), cwd=tmp_path, check=True)
    new_commit = subprocess.check_output(("git", "rev-parse", "HEAD"), cwd=tmp_path, text=True).strip()
    invalidation = invalidate_test_manifest_after_source_bug_v1(
        repository_root=tmp_path,
        state_root_relative="state/chembench_supervised_transfer_v1",
        result_root_relative="results/root",
        failed_run_id="formal-ledger-test-0001",
        current_source_commit=new_commit,
        recorded_at_utc="2026-01-01T00:00:03Z",
    )
    assert invalidation["action"] == "INVALIDATED_BY_IMPLEMENTATION_BUG"
    recovery = _ledger_inputs(
        tmp_path,
        source_commit=new_commit,
        manifest="test_recovery_01",
    )
    frozen_raw = b"frozen receipt\n"
    frozen_sha256 = hashlib.sha256(frozen_raw).hexdigest()
    recovery_claim = claim_test_manifest_use_v1(
        inputs=recovery,
        run_id="formal-ledger-test-0004",
        frozen_receipt_sha256=frozen_sha256,
        claimed_at_utc="2026-01-01T00:00:04Z",
    )
    final_public = tmp_path / "results/root/runs/formal-ledger-test-0004/public"
    final_private = (
        tmp_path
        / "state/chembench_supervised_transfer_v1/runs/formal-ledger-test-0004/private"
    )
    final_public.mkdir(parents=True)
    final_private.mkdir(parents=True)
    final_state = {
        "run_id": "formal-ledger-test-0004",
        "run_mode": "formal",
        "stage": "FINAL_TEST",
        "task_sessions": 4_500,
        "reflector_completions": 900,
        "core_jobs": 2_700,
        "core_artifacts": 2_700,
    }
    (final_public / "run_state.json").write_text(json.dumps(final_state))
    (final_public / "frozen_transfer_receipt_v1.json").write_bytes(frozen_raw)
    (final_public / "test_recovery_01_use_receipt.json").write_text(
        json.dumps({"global_ledger_claim_sha256": recovery_claim["entry_sha256"]})
    )
    public_rows = []
    private_rows = []
    for ordinal in range(450):
        for arm in ("control_test", "online_test"):
            session_id = f"session-{ordinal:03d}-{arm}"
            row = {
                "kind": "TASK_MODEL_EXECUTED",
                "stage": "FINAL_TEST",
                "logical_arm": arm,
                "task_uid": "b",
                "session_id": session_id,
                "memory_artifact_id": None if arm == "control_test" else "artifact",
                "skill_artifact_id": None if arm == "control_test" else "skill",
                "agent_system_artifact_id": (
                    None if arm == "control_test" else "agent-system"
                ),
            }
            public_rows.append(row)
            private_rows.append({**row, "kind": "PRIVATE_EVALUATED"})
    (final_public / "events.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in public_rows)
    )
    (final_private / "events.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in private_rows)
    )
    completion = complete_test_manifest_use_v1(
        inputs=recovery,
        run_id="formal-ledger-test-0004",
        claim_entry_sha256=str(recovery_claim["entry_sha256"]),
        completed_at_utc="2026-01-01T00:00:05Z",
    )
    assert completion["action"] == "COMPLETED"
    ledger = tmp_path / "state/chembench_supervised_transfer_v1" / TEST_LEDGER_FILENAME
    assert ledger.stat().st_mode & 0o777 == 0o600
    assert len(ledger.read_text().splitlines()) == 4
