from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess

import pytest

from openevo_chembench.reflector_execution_boundary_v2 import (
    ReflectorBoundaryStatusV2,
    ReflectorExecutionBoundaryV2,
    TASKWISE_SOURCE_SPLIT,
    _bubblewrap_base_command,
    _create_layout,
    _replace_upstream_paths,
)


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _records(path: Path) -> str:
    records = [
        {
            "uid": hashlib.sha256(f"dev-{index}".encode()).hexdigest(),
            "category": f"category-{index % 9}",
            "source_split": "dev",
            "source_index": index,
            "raw_completion": "A",
            "target": "A" if index % 2 == 0 else "B",
            "correct": index % 2 == 0,
        }
        for index in range(45)
    ]
    path.write_text(
        "".join(
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n" for record in records
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return _canonical_sha256(records)


def _fake_codex(path: Path, event: dict[str, object] | str) -> None:
    event_text = (
        json.dumps(event, sort_keys=True, separators=(",", ":"))
        if isinstance(event, dict)
        else event
    )
    script = f"""#!/bin/sh
set -eu
output=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--output-last-message" ]; then
    output="$2"
    shift 2
  else
    shift
  fi
done
printf '%s\\n' '{event_text}'
printf '%s\\n' '# Do' 'Check constraints.' '# Avoid' 'Avoid lookup.' '# Validate' 'Verify units.' '# When Applicable' 'Use chemistry checks.' '# Retired' 'None.' > "$output"
"""
    path.write_text(script, encoding="utf-8")
    path.chmod(0o700)


def _boundary(
    tmp_path: Path,
    *,
    event: dict[str, object] | str,
) -> tuple[ReflectorExecutionBoundaryV2, Path, Path]:
    source = tmp_path / "private-dev-loo.jsonl"
    digest = _records(source)
    auth = tmp_path / "auth.json"
    auth.write_text("{}\n", encoding="utf-8")
    auth.chmod(0o600)
    fake = tmp_path / "fake-codex"
    _fake_codex(fake, event)
    audit = tmp_path / "private-audit"
    temporary_parent = tmp_path / "temporary"
    temporary_parent.mkdir(mode=0o700)
    return (
        ReflectorExecutionBoundaryV2(
            dev_artifact_path=source,
            expected_records_sha256=digest,
            private_audit_root=audit,
            real_codex_binary=fake,
            auth_source=auth,
            timeout_seconds=30,
            temporary_parent=temporary_parent,
        ),
        audit,
        temporary_parent,
    )


def _upstream_args(work: Path) -> list[str]:
    return [
        "codex",
        "exec",
        "--json",
        "--ignore-user-config",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--disable",
        "shell_tool",
        "--skip-git-repo-check",
        "--cd",
        os.fspath(work),
        "--output-last-message",
        os.fspath(work / "last-message.md"),
        "--model",
        "gpt-5.5",
        "-",
    ]


def test_wrapper_preserves_upstream_args_and_adds_actual_disable_overrides(
    tmp_path: Path,
) -> None:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    original = _upstream_args(upstream)[1:]
    rewritten = _replace_upstream_paths(original)
    assert rewritten[0] == "exec"
    assert rewritten[-1] == "-"
    assert "/output/last-message.md" in rewritten
    assert "/work" in rewritten
    assert "shell_tool" in rewritten
    assert "plugins" in rewritten
    assert "remote_plugin" in rewritten
    assert "multi_agent" in rewritten
    assert 'web_search="disabled"' in rewritten
    assert 'model_reasoning_effort="medium"' in rewritten
    assert 'approval_policy="never"' in rewritten


def test_bubblewrap_visibility_denies_host_sentinels(tmp_path: Path) -> None:
    boundary, _audit, _temporary = _boundary(
        tmp_path,
        event={"type": "turn.completed", "usage": {}},
    )
    sentinels = []
    for name in ("test", "private-pilot", "results"):
        directory = tmp_path / name
        directory.mkdir()
        sentinel = directory / "sentinel"
        sentinel.write_text("must-not-be-visible\n", encoding="utf-8")
        sentinels.append(sentinel)
    repository_sentinel = (
        Path(__file__).resolve().parents[1] / "src" / "openevo_chembench" / "core_evolution_v2.py"
    )
    result = boundary.probe_visibility(
        denied_paths=(
            *sentinels,
            repository_sentinel,
            Path.home() / ".bashrc",
        ),
    )
    assert result == {
        "allowed_dev_artifact_readable": True,
        "all_denied_paths_invisible": True,
        "probe_passed": True,
    }


def test_real_native_codex_mount_policy_excludes_host_system_and_protected_roots(
    tmp_path: Path,
) -> None:
    source = tmp_path / "private-dev-loo.jsonl"
    digest = _records(source)
    auth = tmp_path / "auth.json"
    auth.write_text("{}\n", encoding="utf-8")
    auth.chmod(0o600)
    boundary = ReflectorExecutionBoundaryV2(
        dev_artifact_path=source,
        expected_records_sha256=digest,
        private_audit_root=tmp_path / "audit",
        auth_source=auth,
    )
    assert boundary.real_codex_binary.name == "codex"
    assert boundary.real_codex_binary.read_bytes()[:4] == b"\x7fELF"
    layout_root = tmp_path / "layout"
    layout_root.mkdir()
    layout = _create_layout(layout_root)
    (layout["codex_home"] / "auth.json").write_text("{}\n", encoding="utf-8")
    (layout["inputs"] / "dev_loo_dataset.jsonl").write_bytes(source.read_bytes())
    command = _bubblewrap_base_command(
        bwrap_binary=boundary.bwrap_binary,
        layout=layout,
        executable_source=boundary.real_codex_binary,
        executable_command=("/opt/codex-bin", "--version"),
        include_codex=True,
    )
    bind_sources = {
        Path(command[index + 1]).resolve()
        for index, value in enumerate(command[:-2])
        if value in {"--bind", "--ro-bind"}
    }
    assert Path("/usr") not in bind_sources
    assert Path("/bin") not in bind_sources
    assert Path("/lib") not in bind_sources
    assert Path("/lib64") not in bind_sources
    repository_root = Path(__file__).resolve().parents[3]
    assert not any(
        source_path == repository_root or repository_root in source_path.parents
        for source_path in bind_sources
    )
    completed = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )
    assert completed.returncode == 0
    assert completed.stdout.strip() == "codex-cli 0.144.6"


def test_safe_assistant_event_creates_private_log_and_cleans_root(
    tmp_path: Path,
) -> None:
    boundary, audit, temporary_parent = _boundary(
        tmp_path,
        event={
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "private-memory"},
        },
    )
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    with boundary.activate() as activation:
        assert Path(shutil.which("codex") or "").resolve() == activation.wrapper_path.resolve()
        completed = subprocess.run(
            _upstream_args(upstream),
            input="synthetic prompt",
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
    assert completed.returncode == 0
    receipt = activation.require_receipt()
    assert receipt.status is ReflectorBoundaryStatusV2.COMPLETED
    assert receipt.event_counts == ()
    assert receipt.cleanup_complete is True
    assert (upstream / "last-message.md").is_file()
    event_log = audit / receipt.private_event_reference
    assert stat.S_IMODE(event_log.stat().st_mode) == 0o600
    assert "private-memory" in event_log.read_text(encoding="utf-8")
    assert not list(temporary_parent.glob("openevo-chembench-reflector-v2-*"))


def test_taskwise_two_record_boundary_uses_same_isolated_wrapper(
    tmp_path: Path,
) -> None:
    source = tmp_path / "private-taskwise.jsonl"
    records = [
        {
            "uid": hashlib.sha256(f"taskwise-{index}".encode()).hexdigest(),
            "source_split": TASKWISE_SOURCE_SPLIT,
            "round_index": index,
            "safe_feedback": {"signals": ["incorrect"]},
        }
        for index in range(2)
    ]
    source.write_text(
        "".join(
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n" for record in records
        ),
        encoding="utf-8",
    )
    source.chmod(0o600)
    auth = tmp_path / "auth.json"
    auth.write_text("{}\n", encoding="utf-8")
    auth.chmod(0o600)
    fake = tmp_path / "fake-codex"
    _fake_codex(
        fake,
        {"type": "item.completed", "item": {"type": "agent_message", "text": "safe"}},
    )
    temporary_parent = tmp_path / "temporary"
    temporary_parent.mkdir(mode=0o700)
    boundary = ReflectorExecutionBoundaryV2(
        dev_artifact_path=source,
        expected_records_sha256=_canonical_sha256(records),
        expected_record_count=2,
        expected_source_split=TASKWISE_SOURCE_SPLIT,
        private_audit_root=tmp_path / "audit",
        real_codex_binary=fake,
        auth_source=auth,
        timeout_seconds=30,
        temporary_parent=temporary_parent,
    )
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    with boundary.activate() as activation:
        completed = subprocess.run(
            _upstream_args(upstream),
            input="synthetic taskwise prompt",
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
    assert completed.returncode == 0
    receipt = activation.require_receipt()
    assert receipt.record_count == 2
    assert receipt.ordered_records_sha256 == _canonical_sha256(records)


def test_malformed_event_is_unknown_tool_violation(tmp_path: Path) -> None:
    boundary, _audit, _temporary_parent = _boundary(
        tmp_path,
        event="not-json-private-payload",
    )
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    with boundary.activate() as activation:
        completed = subprocess.run(
            _upstream_args(upstream),
            input="synthetic prompt",
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
    assert completed.returncode != 0
    assert not (upstream / "last-message.md").exists()
    receipt = activation.load_receipt_for_audit()
    assert receipt.status is ReflectorBoundaryStatusV2.SECURITY_TOOL_USE_VIOLATION
    assert dict(receipt.event_counts) == {"unknown_tool": 1}


@pytest.mark.parametrize(
    "item_type,expected_category",
    (
        ("file_read", "file_read"),
        ("file_write", "file_write"),
        ("shell_command", "shell"),
        ("mcp_tool_call", "mcp"),
        ("network_request", "network"),
        ("web_search", "web"),
        ("plugin_call", "plugin"),
        ("app_call", "app"),
        ("subagent_spawn", "subagent"),
        ("novel_tool_operation", "unknown_tool"),
    ),
)
def test_tool_events_fail_closed_without_last_message(
    tmp_path: Path,
    item_type: str,
    expected_category: str,
) -> None:
    boundary, audit, temporary_parent = _boundary(
        tmp_path,
        event={
            "type": "item.started",
            "item": {
                "type": item_type,
                "payload": "private-target-like-event-payload",
            },
        },
    )
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    with boundary.activate() as activation:
        completed = subprocess.run(
            _upstream_args(upstream),
            input="synthetic prompt",
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
    assert completed.returncode != 0
    assert completed.stdout == ""
    assert completed.stderr.strip() == "REFLECTOR_SECURITY_TOOL_USE_VIOLATION"
    assert not (upstream / "last-message.md").exists()
    receipt = activation.load_receipt_for_audit()
    assert receipt.status is ReflectorBoundaryStatusV2.SECURITY_TOOL_USE_VIOLATION
    assert dict(receipt.event_counts) == {expected_category: 1}
    assert receipt.last_message_sha256 is None
    assert receipt.retry_allowed is False
    assert receipt.resume_allowed is False
    assert receipt.replacement_completion_allowed is False
    event_log = audit / receipt.private_event_reference
    assert stat.S_IMODE(event_log.stat().st_mode) == 0o600
    assert "private-target-like-event-payload" in event_log.read_text(encoding="utf-8")
    assert "private-target-like-event-payload" not in json.dumps(receipt.to_payload())
    assert not list(temporary_parent.glob("openevo-chembench-reflector-v2-*"))
    with pytest.raises(RuntimeError, match="REFLECTOR_SECURITY_TOOL_USE_VIOLATION"):
        activation.require_receipt()
