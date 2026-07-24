from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from openevo_chembench.taskwise_config_v1 import (
    CONTROL_PROTOCOL_ID,
    ONLINE_PROTOCOL_ID,
    PROTOCOL_MARKERS,
    SOURCE_COMMIT_PLACEHOLDER,
    load_taskwise_config_v1,
    taskwise_arm_parity_findings,
)
from openevo_chembench.taskwise_sampling_v1 import PILOT500_STREAM_SCOPES


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = PACKAGE_ROOT / "scripts"
CONFIG_ROOT = PACKAGE_ROOT / "configs"
_SUPPORT_SCRIPTS = (
    "generate_taskwise_manifests_v1.sh",
    "dry_run_taskwise_online_v1.sh",
)
_INTERNAL_ENTRYPOINTS = (
    "run_control_canary9_taskwise_online_v1.sh",
    "run_online_canary9_taskwise_online_v1.sh",
    "compare_canary9_taskwise_online_v1.sh",
    "run_control_pilot500_streams_taskwise_online_v1.sh",
    "run_online_pilot500_streams_taskwise_online_v1.sh",
    "compare_pilot500_streams_taskwise_online_v1.sh",
    "run_repeated_control_full_taskwise_v1.sh",
    "run_online_evolution_full_taskwise_v1.sh",
    "compare_online_full_taskwise_v1.sh",
)
_LEGACY_INTERNAL_ENTRYPOINTS = (
    "run_control_pilot500_taskwise_online_v1.sh",
    "run_online_pilot500_taskwise_online_v1.sh",
    "compare_pilot500_taskwise_online_v1.sh",
)
_PUBLIC_ENTRYPOINTS = {
    "run_repeated_control_canary9_v1.sh": "run_control_canary9_taskwise_online_v1.sh",
    "run_online_evolution_canary9_v1.sh": "run_online_canary9_taskwise_online_v1.sh",
    "compare_online_canary9_v1.sh": "compare_canary9_taskwise_online_v1.sh",
    "run_repeated_control_pilot500_v1.sh": ("run_control_pilot500_streams_taskwise_online_v1.sh"),
    "run_online_evolution_pilot500_v1.sh": ("run_online_pilot500_streams_taskwise_online_v1.sh"),
    "compare_online_pilot500_v1.sh": ("compare_pilot500_streams_taskwise_online_v1.sh"),
}


def test_taskwise_scripts_are_executable_and_syntactically_valid() -> None:
    for name in (
        *_SUPPORT_SCRIPTS,
        *_INTERNAL_ENTRYPOINTS,
        *_LEGACY_INTERNAL_ENTRYPOINTS,
        *_PUBLIC_ENTRYPOINTS,
    ):
        path = SCRIPTS_ROOT / name
        assert path.is_file()
        assert os.access(path, os.X_OK)
        subprocess.run(
            ("bash", "-n", os.fspath(path)),
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )


def test_manifest_wrapper_forwards_explicit_read_only_command() -> None:
    text = (SCRIPTS_ROOT / "generate_taskwise_manifests_v1.sh").read_text(encoding="utf-8")
    assert 'command="${1:-generate}"' in text
    assert 'run_taskwise_manifest_tool "${command}" "$@"' in text
    assert "run_taskwise_manifest_tool generate" not in text


def test_manifest_wrapper_dry_run_is_read_only() -> None:
    public_manifest = (
        PACKAGE_ROOT / "manifests" / "taskwise_online_v1" / "online_canary9_public_manifest.jsonl"
    )
    before_bytes = public_manifest.read_bytes()
    before_mtime_ns = public_manifest.stat().st_mtime_ns
    completed = subprocess.run(
        (os.fspath(SCRIPTS_ROOT / "generate_taskwise_manifests_v1.sh"), "dry-run"),
        cwd=PACKAGE_ROOT.parents[1],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
    )
    payload = json.loads(completed.stdout)
    assert payload["operation"] == "dry-run"
    assert payload["model_calls"] == 0
    assert public_manifest.read_bytes() == before_bytes
    assert public_manifest.stat().st_mtime_ns == before_mtime_ns
    assert completed.stderr == ""


def test_paid_entrypoints_dispatch_only_through_the_taskwise_cli() -> None:
    for name in _INTERNAL_ENTRYPOINTS:
        text = (SCRIPTS_ROOT / name).read_text(encoding="utf-8")
        assert "codex exec" not in text
        assert "require_taskwise_source_commit" in text
        assert "run_taskwise_cli" in text
    for name, target in _PUBLIC_ENTRYPOINTS.items():
        text = (SCRIPTS_ROOT / name).read_text(encoding="utf-8")
        assert "codex exec" not in text
        assert f'exec "${{SCRIPT_DIR}}/{target}" "$@"' in text


def test_current_paid_entrypoint_stops_at_dirty_source_gate_without_model_call(
    tmp_path: Path,
) -> None:
    dirty_probe = PACKAGE_ROOT / f"paid_entrypoint_dirty_probe_{tmp_path.name}.txt"
    descriptor = os.open(dirty_probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    try:
        completed = subprocess.run(
            (os.fspath(SCRIPTS_ROOT / "run_repeated_control_canary9_v1.sh"),),
            cwd=PACKAGE_ROOT.parents[1],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
    finally:
        dirty_probe.unlink(missing_ok=True)

    assert completed.returncode != 0
    assert "TASKWISE_PACKAGE_SOURCE_DIRTY" in completed.stderr
    assert "TASKWISE_ONLINE_CLI_NOT_INSTALLED" not in completed.stderr


def test_control_and_online_configs_have_only_registered_treatment_differences() -> None:
    for scope in ("canary9", *PILOT500_STREAM_SCOPES):
        control = load_taskwise_config_v1(CONFIG_ROOT / f"control_{scope}_taskwise_online_v1.yaml")
        online = load_taskwise_config_v1(CONFIG_ROOT / f"online_{scope}_taskwise_online_v1.yaml")

        assert taskwise_arm_parity_findings(control, online) == ()
        assert control.protocol_id == CONTROL_PROTOCOL_ID
        assert online.protocol_id == ONLINE_PROTOCOL_ID
        assert control.protocol_markers == online.protocol_markers == PROTOCOL_MARKERS
        assert control.attempts_per_task == online.attempts_per_task == 3
        assert control.inter_round_slots == online.inter_round_slots == 2
        assert control.fixed_round_budget is online.fixed_round_budget is True
        assert control.stop_when_correct is online.stop_when_correct is False
        assert control.evolution_updates_per_task == 0
        assert online.evolution_updates_per_task == 2
        assert control.carry_memory_across_tasks is False
        assert online.carry_memory_across_tasks is True
        assert control.evolution.method_id is None
        assert online.evolution.method_id == "text_memory_expel_reflector"
        assert control.task_manifest == online.task_manifest
        assert control.source_commit == online.source_commit == SOURCE_COMMIT_PLACEHOLDER


def test_each_pilot_stream_has_independent_output_and_core_namespace() -> None:
    outputs: set[str] = set()
    run_names: set[str] = set()
    for scope in PILOT500_STREAM_SCOPES:
        control = load_taskwise_config_v1(CONFIG_ROOT / f"control_{scope}_taskwise_online_v1.yaml")
        online = load_taskwise_config_v1(CONFIG_ROOT / f"online_{scope}_taskwise_online_v1.yaml")
        assert control.scope == online.scope == scope
        assert control.task_manifest == online.task_manifest
        assert control.private_task_manifest == online.private_task_manifest
        for config in (control, online):
            assert config.output_directory not in outputs
            assert config.run_name not in run_names
            outputs.add(config.output_directory)
            run_names.add(config.run_name)
    online_script = (SCRIPTS_ROOT / "run_online_pilot500_streams_taskwise_online_v1.sh").read_text(
        encoding="utf-8"
    )
    control_script = (
        SCRIPTS_ROOT / "run_control_pilot500_streams_taskwise_online_v1.sh"
    ).read_text(encoding="utf-8")
    assert "run-pilot-stream-suite --arm online" in online_script
    assert "run-pilot-stream-suite --arm control" in control_script
    assert "fresh runner" in online_script
    assert "generation-zero memory" in online_script
    assert "selectively resumes" not in online_script
    assert '"$@"' in online_script
    assert '"$@"' in control_script


def test_dry_run_performs_manifest_and_config_validation_without_model_calls() -> None:
    completed = subprocess.run(
        (os.fspath(SCRIPTS_ROOT / "dry_run_taskwise_online_v1.sh"),),
        cwd=PACKAGE_ROOT.parents[1],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
    )

    assert '"status": "PASS"' in completed.stdout
    assert '"model_calls": 0' in completed.stdout
    assert '"item_count": 9' in completed.stdout
    payload = json.loads(completed.stdout)
    assert all(payload["scopes"][scope]["item_count"] == 50 for scope in PILOT500_STREAM_SCOPES)
    assert '"total_item_count": 500' in completed.stdout
    assert '"reset_memory_between_streams": true' in completed.stdout
    assert '"parity_findings": []' in completed.stdout
    assert completed.stderr == ""
