from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import openevo_chembench.taskwise_cli_v1 as cli
import openevo_chembench.taskwise_pilot_go_receipt_v1 as gate
from openevo_chembench.chembench4k_dataset import ChemBench4KDatasetLoader
from openevo_chembench.chembench4k_models import CHEMBENCH4K_CATEGORIES
from openevo_chembench.taskwise_canary_receipt_v1 import (
    TaskwisePairedCanaryReceiptInputsV1,
)
from openevo_chembench.taskwise_config_v1 import load_taskwise_config_v1
from openevo_chembench.taskwise_generation_v1 import (
    derive_taskwise_generation_id_v1,
    taskwise_full_comparison_path_v1,
    taskwise_full_runtime_config_v1,
    taskwise_full_suite_state_path_v1,
)
from openevo_chembench.taskwise_pilot_go_receipt_v1 import (
    TaskwiseFullAuthorizationV1,
    TaskwisePilotGOFindingV1,
    TaskwisePilotGOReceiptInputsV1,
    TaskwisePilotGOReceiptV1,
    verify_taskwise_pilot_go_receipt_v1,
)
from openevo_chembench.taskwise_reporting_v1 import PrivateTaskwiseRoundResultV1
from openevo_chembench.taskwise_sampling_v1 import (
    FULL_SIZE,
    FULL_STREAM_SCOPES,
    FULL_STREAM_TASK_COUNTS,
)
from openevo_chembench.taskwise_stream_statistics_v1 import (
    PrivateTaskwisePairedFullStreamV1,
    build_taskwise_full_stream_report_v1,
)


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = PACKAGE_ROOT / "scripts"
CONFIG_ROOT = PACKAGE_ROOT / "configs"
_SOURCE_COMMIT = "a" * 40
_PILOT_GENERATION = f"gen_{'1' * 64}"
_FULL_GENERATION = f"gen_{'2' * 64}"
_ATTEMPT = "attempt_000001"


def _receipt() -> TaskwisePilotGOReceiptV1:
    return TaskwisePilotGOReceiptV1(
        fields={
            "source_commit": _SOURCE_COMMIT,
            "canary_receipt_sha256": "1" * 64,
            "pilot_generation_id": _PILOT_GENERATION,
            "pilot_attempt_id": _ATTEMPT,
            "pilot_report_sha256": "2" * 64,
            "full_binding_sha256": "3" * 64,
            "full_generation_id": _FULL_GENERATION,
        },
        finding_codes=(),
        created_at_utc="2026-07-24T00:00:00+00:00",
    )


def _inputs(tmp_path: Path) -> TaskwisePilotGOReceiptInputsV1:
    canary = TaskwisePairedCanaryReceiptInputsV1(
        repository_root=tmp_path,
        package_root=tmp_path / "benchmarks" / "chembench",
        dataset_root=tmp_path / "dataset",
        source_manifest_path=tmp_path / "source.json",
        framework_lock_path=tmp_path / "framework.json",
        control_config_path=tmp_path / "control.yaml",
        online_config_path=tmp_path / "online.yaml",
    )
    return TaskwisePilotGOReceiptInputsV1(
        repository_root=tmp_path,
        package_root=tmp_path / "benchmarks" / "chembench",
        dataset_root=tmp_path / "dataset",
        source_manifest_path=tmp_path / "source.json",
        canary_inputs=canary,
    )


def test_full_authorization_cannot_be_constructed_from_caller_boolean() -> None:
    with pytest.raises(TypeError, match="issued only"):
        TaskwiseFullAuthorizationV1(
            receipt_sha256="0" * 64,
            evidence_digest="1" * 64,
            source_commit=_SOURCE_COMMIT,
            canary_receipt_sha256="2" * 64,
            pilot_generation_id=_PILOT_GENERATION,
            pilot_attempt_id=_ATTEMPT,
            pilot_report_sha256="3" * 64,
            full_binding_sha256="4" * 64,
            full_generation_id=_FULL_GENERATION,
        )


def test_full_authority_recomputes_stored_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _receipt()
    path = tmp_path / "pilot_go_receipt.json"
    path.write_bytes(receipt.canonical_bytes())
    path.chmod(0o600)
    monkeypatch.setattr(
        gate,
        "recompute_taskwise_pilot_go_receipt_v1",
        lambda _inputs: receipt,
    )

    authorization = verify_taskwise_pilot_go_receipt_v1(_inputs(tmp_path), path)

    assert authorization.full_generation_id == _FULL_GENERATION
    assert authorization.pilot_generation_id == _PILOT_GENERATION
    assert authorization.pilot_attempt_id == _ATTEMPT
    assert authorization.receipt_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


def test_pilot_no_go_cannot_issue_full_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = {
        "schema_version": "taskwise_online_pilot500_ten_stream_report_v1",
        "status": "COMPLETED",
        "decision": "NO_GO",
        "completed_streams": 10,
        "stream_count": 10,
        "task_count": 500,
    }
    monkeypatch.setattr(gate, "_git", lambda *_args: "")
    monkeypatch.setattr(gate, "verify_source_manifest", lambda *_args: "4" * 64)
    monkeypatch.setattr(
        gate,
        "default_taskwise_canary_receipt_path_v1",
        lambda *_args: tmp_path / "canary.json",
    )
    monkeypatch.setattr(
        gate,
        "verify_taskwise_paired_canary_receipt_v1",
        lambda *_args: SimpleNamespace(
            receipt_sha256="1" * 64,
            evidence_digest="2" * 64,
            paired_canary_generation_id=_PILOT_GENERATION,
            pilot_binding_sha256="3" * 64,
            source_commit=None,
        ),
    )
    monkeypatch.setattr(cli, "compare_pilot500_streams", lambda **_kwargs: report)
    monkeypatch.setattr(
        gate,
        "require_taskwise_completed_paired_attempt_v1",
        lambda *_args, **_kwargs: _ATTEMPT,
    )
    monkeypatch.setattr(
        gate, "_read_regular", lambda *_args, **_kwargs: gate._canonical_bytes(report)
    )
    monkeypatch.setattr(
        gate,
        "ChemBench4KDatasetLoader",
        lambda **_kwargs: SimpleNamespace(manifest=SimpleNamespace(combined_sha256="5" * 64)),
    )
    monkeypatch.setattr(
        gate,
        "_recompute_full_binding",
        lambda *_args: {
            "full_binding_sha256": "6" * 64,
            "full_suite_summary_sha256": "7" * 64,
            "full_ordered_uid_sha256": "8" * 64,
            "full_config_manifest_sha256": "9" * 64,
        },
    )

    receipt = gate.recompute_taskwise_pilot_go_receipt_v1(_inputs(tmp_path))

    assert receipt.paid_full_allowed is False
    assert TaskwisePilotGOFindingV1.PILOT_DECISION_NOT_GO in receipt.finding_codes


def test_full_generation_paths_and_runtime_are_private() -> None:
    generation = derive_taskwise_generation_id_v1({"full": "authority"})
    template = load_taskwise_config_v1(
        CONFIG_ROOT / "online_full_stream_00_taskwise_online_v1.yaml"
    )
    runtime = taskwise_full_runtime_config_v1(template, generation)

    assert generation in runtime.run_name
    assert generation in runtime.output_directory
    assert runtime.scope == "full_stream_00"
    assert generation in taskwise_full_comparison_path_v1(PACKAGE_ROOT, generation).parts
    assert (
        generation
        in taskwise_full_suite_state_path_v1(
            PACKAGE_ROOT,
            generation_id=generation,
            arm="online",
        ).parts
    )


def test_full_binding_recomputes_all_frozen_configs_and_manifests() -> None:
    inputs = gate.default_taskwise_pilot_go_receipt_inputs_v1(PACKAGE_ROOT)
    loader = ChemBench4KDatasetLoader(snapshot_root=inputs.dataset_root)

    binding = gate._recompute_full_binding(inputs, loader)

    assert binding == {
        "full_binding_sha256": (
            "a3f7cc1328acae15f31bd005dd1c94eb24610b997b085c18187acdfeb64a2d8b"
        ),
        "full_suite_summary_sha256": (
            "85feff14bef0423c15be382fe8ee000d433cf743151526b470ef324d423594d5"
        ),
        "full_ordered_uid_sha256": (
            "d4e0e72ab7f7d4b674cdea3ba6ef12f1d8e1041c4257b23a91239727d728fd6e"
        ),
        "full_config_manifest_sha256": (
            "34835d18255a99c143881a31c20eaf2cbc72a5a8c9ced04d1979127544dc795c"
        ),
    }


def _full_rows(
    *,
    stream_index: int,
    task_count: int,
    arm: str,
    prediction: str,
) -> tuple[PrivateTaskwiseRoundResultV1, ...]:
    rows: list[PrivateTaskwiseRoundResultV1] = []
    prior_artifact: str | None = None
    prior_memory: str | None = None
    for task_index in range(task_count):
        uid = hashlib.sha256(f"full-stream-{stream_index}-task-{task_index}".encode()).hexdigest()
        category = CHEMBENCH4K_CATEGORIES[
            (stream_index * 401 + task_index) % len(CHEMBENCH4K_CATEGORIES)
        ]
        for round_index in (0, 1, 2):
            artifact = None
            memory = None
            if arm == "online":
                if round_index == 0:
                    artifact = prior_artifact
                    memory = prior_memory
                else:
                    artifact = f"artifact_{stream_index}_{task_index}_{round_index}"
                    memory = hashlib.sha256(artifact.encode()).hexdigest()
            rows.append(
                PrivateTaskwiseRoundResultV1(
                    task_uid=uid,
                    task_index=task_index,
                    category=category,
                    arm=arm,  # type: ignore[arg-type]
                    round_index=round_index,  # type: ignore[arg-type]
                    target="A",
                    official_prediction=prediction,
                    official_parsed=True,
                    strict_parsed=True,
                    core_artifact_id=artifact,
                    memory_digest=memory,
                )
            )
        if arm == "online":
            prior_artifact = f"artifact_{stream_index}_{task_index}_2"
            prior_memory = hashlib.sha256(prior_artifact.encode()).hexdigest()
    return tuple(rows)


def test_full_comparison_supports_variable_400_401_streams() -> None:
    streams = tuple(
        PrivateTaskwisePairedFullStreamV1(
            stream_id=scope,
            control=_full_rows(
                stream_index=index,
                task_count=task_count,
                arm="control",
                prediction="B",
            ),
            online=_full_rows(
                stream_index=index,
                task_count=task_count,
                arm="online",
                prediction="A",
            ),
            approved_memory_utf8_bytes=tuple(range(task_count * 2)),
        )
        for index, (scope, task_count) in enumerate(
            zip(FULL_STREAM_SCOPES, FULL_STREAM_TASK_COUNTS, strict=True)
        )
    )

    report = build_taskwise_full_stream_report_v1(streams)

    assert report["status"] == "COMPLETED"
    assert report["task_count"] == FULL_SIZE == 4009
    assert sorted(report["per_stream_task_count"].values()) == [400, *([401] * 9)]
    assert report["paired_final_round"]["absolute_delta"] == 1.0
    positions = report["dependent_task_observations"]["round_0_accuracy_by_within_stream_position"]
    assert len(positions) == 401
    assert positions[-1]["stream_observations"] == 9


def test_full_comparison_rejects_incomplete_or_violating_streams() -> None:
    streams = tuple(
        PrivateTaskwisePairedFullStreamV1(
            stream_id=scope,
            control=_full_rows(
                stream_index=index,
                task_count=task_count,
                arm="control",
                prediction="B",
            ),
            online=_full_rows(
                stream_index=index,
                task_count=task_count,
                arm="online",
                prediction="A",
            ),
            approved_memory_utf8_bytes=tuple(range(task_count * 2)),
        )
        for index, (scope, task_count) in enumerate(
            zip(FULL_STREAM_SCOPES, FULL_STREAM_TASK_COUNTS, strict=True)
        )
    )

    with pytest.raises(ValueError, match="violation-free"):
        build_taskwise_full_stream_report_v1((replace(streams[0], completed=False), *streams[1:]))
    with pytest.raises(ValueError, match="violation-free"):
        build_taskwise_full_stream_report_v1(
            (replace(streams[0], online_security_violations=1), *streams[1:])
        )


def test_full_suite_rejects_existing_stream_without_resume_or_stitch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "FULL4009_AUTHORIZATION", "GRANTED")
    monkeypatch.setattr(cli, "FULL4009_EXECUTION_ALLOWED", True)
    monkeypatch.setattr(cli, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(cli, "_verify_static_inputs", lambda *_args: None)
    monkeypatch.setattr(
        cli,
        "require_taskwise_full_authorization_v1",
        lambda: SimpleNamespace(
            full_generation_id=_FULL_GENERATION,
            pilot_generation_id=_PILOT_GENERATION,
            pilot_attempt_id=_ATTEMPT,
            receipt_sha256="1" * 64,
            pilot_report_sha256="2" * 64,
            full_binding_sha256="3" * 64,
        ),
    )
    monkeypatch.setattr(
        cli,
        "_authorized_full_generation_v1",
        lambda _authorization, requested: requested or _FULL_GENERATION,
    )
    template = load_taskwise_config_v1(
        CONFIG_ROOT / "control_full_stream_00_taskwise_online_v1.yaml"
    )
    runtime = taskwise_full_runtime_config_v1(template, _FULL_GENERATION)
    output = tmp_path / runtime.output_directory
    output.mkdir(parents=True)
    (output / "run_state.json").write_bytes(
        cli._canonical_bytes(
            {
                "schema_version": "taskwise_online_run_state_v1",
                "arm": "control",
                "status": "COMPLETED",
                "standard_chembench4k_score_claimed": False,
            }
        )
    )
    calls: list[object] = []

    with pytest.raises(cli.TaskwiseCLIError, match="SUITE_TERMINAL_FAILURE"):
        cli.run_full_stream_suite(
            arm="control",
            arm_runner=lambda *_args, **_kwargs: calls.append(object()),
            suite_state_path=tmp_path / "suite" / "control.json",
            source_gate=lambda _config: _SOURCE_COMMIT,
        )

    assert calls == []
    state = json.loads((tmp_path / "suite" / "control.json").read_text())
    assert state["status"] == "FAILED"
    assert state["streams"]["full_stream_00"]["action"] == "TERMINAL_FAILURE"


def test_all_full_entrypoints_fail_closed_without_explicit_user_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    full_config = cli.CONFIG_ROOT / "online_full_stream_00_taskwise_online_v1.yaml"
    touched = {"authorization": 0, "source": 0, "runner": 0}

    def forbidden_authorization() -> object:
        touched["authorization"] += 1
        raise AssertionError("legacy receipt must not grant 4009-item authority")

    def forbidden_source(_config: object) -> str:
        touched["source"] += 1
        raise AssertionError("source preflight must not run before the user gate")

    def forbidden_runner(*_args: object, **_kwargs: object) -> object:
        touched["runner"] += 1
        raise AssertionError("paid full runner must remain unreachable")

    monkeypatch.setattr(
        cli,
        "require_taskwise_full_authorization_v1",
        forbidden_authorization,
    )

    with pytest.raises(
        cli.TaskwiseCLIError,
        match="USER_FULL_RUN_AUTHORIZATION_MISSING",
    ):
        cli.run_arm(
            full_config,
            source_gate=forbidden_source,
            executor_factory=forbidden_runner,
            core_port_factory=forbidden_runner,
        )

    with pytest.raises(
        cli.TaskwiseCLIError,
        match="USER_FULL_RUN_AUTHORIZATION_MISSING",
    ):
        cli.run_full_stream_suite(
            arm="online",
            arm_runner=forbidden_runner,
            suite_state_path=tmp_path / "suite.json",
            source_gate=forbidden_source,
        )

    assert touched == {"authorization": 0, "source": 0, "runner": 0}


def test_full_entrypoints_recompute_go_receipt_and_never_resume() -> None:
    launchers = (
        "run_repeated_control_full_taskwise_v1.sh",
        "run_online_evolution_full_taskwise_v1.sh",
    )
    for name in launchers:
        path = SCRIPTS_ROOT / name
        assert path.is_file()
        assert os.access(path, os.X_OK)
        text = path.read_text(encoding="utf-8")
        assert "verify-pilot-go-receipt" in text
        assert "run-full-stream-suite" in text
        assert "--resume" not in text
        assert "skip" not in text.casefold()
        assert "codex exec" not in text
    compare = (SCRIPTS_ROOT / "compare_online_full_taskwise_v1.sh").read_text()
    assert "verify-pilot-go-receipt" in compare
    assert "compare-full-streams" in compare
    assert (
        "freeze-pilot-go-receipt"
        in (SCRIPTS_ROOT / "compare_pilot500_streams_taskwise_online_v1.sh").read_text()
    )


def test_private_full_report_storage_is_generation_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = derive_taskwise_generation_id_v1({"storage": "full"})
    monkeypatch.setattr(cli, "REPOSITORY_ROOT", tmp_path)
    path = taskwise_full_comparison_path_v1(tmp_path, generation)
    cli._persist_private_comparison_report_v1(path, {"status": "COMPLETED"})

    assert stat.S_IMODE(path.lstat().st_mode) == 0o600
    assert path.read_bytes() == cli._canonical_bytes({"status": "COMPLETED"})
