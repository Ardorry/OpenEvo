from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

import openevo_chembench.taskwise_cli_v1 as cli
from openevo_chembench.taskwise_config_v1 import load_taskwise_config_v1
from openevo_chembench.taskwise_generation_v1 import (
    derive_taskwise_generation_id_v1,
    taskwise_canary_comparison_path_v1,
    taskwise_canary_receipt_path_v1,
    taskwise_pilot_comparison_path_v1,
    taskwise_pilot_runtime_config_v1,
    taskwise_pilot_suite_state_path_v1,
    validate_taskwise_generation_id_v1,
)


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PACKAGE_ROOT.parents[1]


def test_generation_id_and_paths_are_byte_deterministic() -> None:
    authority = {
        "source_commit": "a" * 40,
        "control_run_id": "control_generation_a",
        "online_run_id": "online_generation_a",
    }
    first = derive_taskwise_generation_id_v1(authority)
    second = derive_taskwise_generation_id_v1(dict(reversed(tuple(authority.items()))))

    assert first == second
    assert validate_taskwise_generation_id_v1(first) == first
    assert taskwise_canary_receipt_path_v1(PACKAGE_ROOT, first) == (
        PACKAGE_ROOT
        / "state"
        / "taskwise_online_v1"
        / "canary9"
        / "generations"
        / first
        / "paired_canary_receipt_v1.json"
    )
    assert first in taskwise_canary_comparison_path_v1(REPOSITORY_ROOT, first).parts
    assert first in taskwise_pilot_comparison_path_v1(REPOSITORY_ROOT, first).parts
    assert (
        first
        in taskwise_pilot_suite_state_path_v1(
            REPOSITORY_ROOT,
            generation_id=first,
            arm="online",
        ).parts
    )


def test_generation_namespaces_preserve_old_evidence(tmp_path: Path) -> None:
    old_generation = derive_taskwise_generation_id_v1({"generation": "old"})
    new_generation = derive_taskwise_generation_id_v1({"generation": "new"})
    old_path = taskwise_canary_receipt_path_v1(tmp_path, old_generation)
    new_path = taskwise_canary_receipt_path_v1(tmp_path, new_generation)
    old_path.parent.mkdir(parents=True)
    old_path.write_text("immutable-old-evidence", encoding="utf-8")

    assert old_path != new_path
    assert old_path.read_text(encoding="utf-8") == "immutable-old-evidence"
    assert not new_path.exists()


def test_pilot_runtime_namespace_is_generation_private() -> None:
    template = load_taskwise_config_v1(
        PACKAGE_ROOT / "configs" / "online_pilot500_stream_00_taskwise_online_v1.yaml"
    )
    first = derive_taskwise_generation_id_v1({"attempt": 1})
    second = derive_taskwise_generation_id_v1({"attempt": 2})
    runtime_first = taskwise_pilot_runtime_config_v1(template, first)
    runtime_second = taskwise_pilot_runtime_config_v1(template, second)

    assert runtime_first.scope == runtime_second.scope == template.scope
    assert runtime_first.task_manifest == runtime_second.task_manifest == template.task_manifest
    assert (
        runtime_first.private_task_manifest
        == runtime_second.private_task_manifest
        == template.private_task_manifest
    )
    assert runtime_first.run_name != runtime_second.run_name
    assert runtime_first.output_directory != runtime_second.output_directory
    assert first in runtime_first.run_name
    assert first in runtime_first.output_directory


@pytest.mark.parametrize(
    "value",
    ("", "generation_abc", "gen_abc", f"gen_{'A' * 64}", "../gen_" + "a" * 64),
)
def test_generation_id_rejects_unsafe_or_noncanonical_values(value: str) -> None:
    with pytest.raises(ValueError, match="generation id"):
        validate_taskwise_generation_id_v1(value)


@pytest.mark.parametrize("scope", ("canary", "pilot"))
def test_fresh_nested_generation_private_report_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
) -> None:
    generation = derive_taskwise_generation_id_v1({"scope": scope})
    monkeypatch.setattr(cli, "REPOSITORY_ROOT", tmp_path)
    path = (
        taskwise_canary_comparison_path_v1(tmp_path, generation)
        if scope == "canary"
        else taskwise_pilot_comparison_path_v1(tmp_path, generation)
    )
    report = {"schema_version": "test_private_comparison", "status": "PASS"}

    cli._persist_private_comparison_report_v1(path, report)

    assert path.read_bytes() == cli._canonical_bytes(report)
    assert stat.S_IMODE(path.lstat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.lstat().st_mode) == 0o700
    assert stat.S_IMODE(path.parent.parent.lstat().st_mode) == 0o700


def test_generation_private_report_rejects_symlink_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = derive_taskwise_generation_id_v1({"unsafe": "symlink"})
    monkeypatch.setattr(cli, "REPOSITORY_ROOT", tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "results").symlink_to(outside, target_is_directory=True)
    path = taskwise_canary_comparison_path_v1(tmp_path, generation)

    with pytest.raises(cli.TaskwiseCLIError, match="COMPARISON_STORAGE_INVALID"):
        cli._persist_private_comparison_report_v1(path, {"status": "PASS"})

    assert not (outside / "chembench4k_taskwise_online_v1").exists()


def test_generation_private_report_rejects_unsafe_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = derive_taskwise_generation_id_v1({"unsafe": "permissions"})
    monkeypatch.setattr(cli, "REPOSITORY_ROOT", tmp_path)
    path = taskwise_pilot_comparison_path_v1(tmp_path, generation)
    generation_root = path.parent.parent
    generation_root.mkdir(parents=True, mode=0o700)
    generation_root.chmod(0o755)

    with pytest.raises(cli.TaskwiseCLIError, match="COMPARISON_STORAGE_INVALID"):
        cli._persist_private_comparison_report_v1(path, {"status": "PASS"})

    assert not path.exists()


def test_existing_generation_report_is_never_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = derive_taskwise_generation_id_v1({"immutable": True})
    monkeypatch.setattr(cli, "REPOSITORY_ROOT", tmp_path)
    path = taskwise_canary_comparison_path_v1(tmp_path, generation)
    original = {"status": "ORIGINAL"}
    cli._persist_private_comparison_report_v1(path, original)
    original_bytes = path.read_bytes()

    with pytest.raises(cli.TaskwiseCLIError, match="COMPARISON_OUTPUT_EXISTS"):
        cli._persist_private_comparison_report_v1(path, {"status": "REPLACEMENT"})

    assert path.read_bytes() == original_bytes


def test_comparison_storage_oserror_is_closed_at_cli_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sensitive_path = tmp_path / "must-not-appear"

    def fail_parent(_path: Path) -> int:
        raise PermissionError(f"denied: {sensitive_path}")

    def fail_compare(**_kwargs: object) -> dict[str, object]:
        monkeypatch.setattr(cli, "_open_private_comparison_parent_v1", fail_parent)
        cli._persist_private_comparison_report_v1(
            sensitive_path,
            {"status": "PASS"},
        )
        raise AssertionError("unreachable")

    monkeypatch.setattr(cli, "compare", fail_compare)
    assert cli.main(["compare", "--scope", "canary9"]) == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert captured.out == ""
    assert payload["error_type"] == "TASKWISE_COMPARISON_STORAGE_INVALID"
    assert "Traceback" not in captured.err
    assert str(sensitive_path) not in captured.err
