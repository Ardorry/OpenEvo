from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import time

import pytest

from openevo_chembench.reflector_execution_boundary_v2 import (
    ReflectorBoundaryError,
    ReflectorBoundaryStatusV2,
    ReflectorExecutionBoundaryV2,
    ReflectorExecutionReceiptV2,
    TASKWISE_SOURCE_SPLIT,
    _bubblewrap_base_command,
    _create_layout,
    _materialize_reflector_transport_environment,
    _project_reflector_prompt,
    _reflector_hardening_arguments,
    _replace_upstream_paths,
    _run_process_group,
    inspect_reflector_codex_policy_v2,
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
    item = event.get("item") if isinstance(event, dict) else None
    last_message = (
        str(item["text"])
        if isinstance(item, dict)
        and item.get("type") == "agent_message"
        and type(item.get("text")) is str
        else (
            "# Do\nCheck constraints.\n# Avoid\nAvoid lookup.\n"
            "# Validate\nVerify units.\n# When Applicable\n"
            "Use chemistry checks.\n# Retired\nNone."
        )
    )
    thread_started = json.dumps(
        {"type": "thread.started", "thread_id": "thread-safe"},
        sort_keys=True,
        separators=(",", ":"),
    )
    turn_started = json.dumps(
        {"type": "turn.started"},
        sort_keys=True,
        separators=(",", ":"),
    )
    turn_completed = json.dumps(
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 1,
                "cached_input_tokens": 0,
                "output_tokens": 1,
                "reasoning_output_tokens": 0,
            },
        },
        sort_keys=True,
        separators=(",", ":"),
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
printf '%s\\n' '{thread_started}' '{turn_started}' '{event_text}' '{turn_completed}'
printf '%s\\n' {shlex.quote(last_message)} > "$output"
"""
    path.write_text(script, encoding="utf-8")
    path.chmod(0o700)


def _fake_failing_codex(path: Path) -> str:
    stderr = "Error loading config.toml: invalid type: boolean false, expected mapping\n"
    path.write_text(
        f"#!/bin/sh\nprintf '%s' '{stderr}' >&2\nexit 1\n",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return stderr


def _passing_policy_probe(
    arguments,
    _cwd: Path,
    _environment,
    _timeout_seconds: float,
) -> tuple[int, str]:
    command = tuple(arguments)
    if command[1:] == ("--version",):
        return 0, "codex-cli 0.145.0\n"
    assert command[1:3] == ("debug", "prompt-input")
    assert "exec" not in command
    assert "--model" not in command
    return 0, "discarded config parser output"


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
            config_probe_runner=_passing_policy_probe,
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
    assert "agents.enabled=false" not in rewritten


@pytest.mark.parametrize(
    "extra_arguments",
    (
        ("--enable", "shell_tool"),
        ("--add-dir", "/"),
        ("--config", 'approval_policy="on-request"'),
        ("--sandbox", "danger-full-access"),
        ("--search",),
    ),
)
def test_wrapper_rejects_any_unapproved_upstream_argument(
    tmp_path: Path,
    extra_arguments: tuple[str, ...],
) -> None:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    arguments = _upstream_args(upstream)[1:-1] + [*extra_arguments, "-"]

    boundary, _audit, _temporary_parent = _boundary(
        tmp_path,
        event={
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "safe"},
        },
    )
    with boundary.activate() as activation:
        completed = subprocess.run(
            [os.fspath(activation.wrapper_path), *arguments],
            input="synthetic prompt",
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )

    assert completed.returncode != 0
    assert completed.stdout == ""
    assert not (upstream / "last-message.md").exists()


def test_reflector_hardening_has_no_agents_enabled_override() -> None:
    arguments = _reflector_hardening_arguments()
    config_values = [
        arguments[index + 1] for index, value in enumerate(arguments[:-1]) if value == "--config"
    ]
    assert config_values == [
        'model_reasoning_effort="medium"',
        'web_search="disabled"',
        'approval_policy="never"',
        'forced_login_method="chatgpt"',
        "check_for_update_on_startup=false",
        "allow_login_shell=false",
        'shell_environment_policy.inherit="none"',
    ]
    assert all(not value.startswith("agents.") for value in config_values)


def test_reflector_config_probe_is_zero_model_and_discards_subprocess_text(
    tmp_path: Path,
) -> None:
    fake_binary = tmp_path / "codex"
    fake_binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_binary.chmod(0o700)
    calls: list[tuple[str, ...]] = []

    def fake_probe(
        arguments,
        cwd: Path,
        environment,
        _timeout_seconds: float,
    ) -> tuple[int, str]:
        command = tuple(arguments)
        calls.append(command)
        assert not any(cwd.iterdir())
        assert Path(environment["HOME"]).is_dir()
        assert Path(environment["CODEX_HOME"]).is_dir()
        assert Path(environment["XDG_CONFIG_HOME"]).is_dir()
        assert Path(environment["XDG_CACHE_HOME"]).is_dir()
        assert Path(environment["XDG_STATE_HOME"]).is_dir()
        assert Path(environment["TMPDIR"]).is_dir()
        if command[1:] == ("--version",):
            return 0, "codex-cli 0.145.0\n"
        assert command[1:3] == ("debug", "prompt-input")
        assert "exec" not in command
        assert "--model" not in command
        return 0, "PRIVATE OUTPUT MUST NOT ENTER RECEIPT"

    receipt = inspect_reflector_codex_policy_v2(
        fake_binary,
        probe_runner=fake_probe,
    )

    assert receipt["status"] == "PASS"
    assert receipt["finding_codes"] == []
    assert receipt["model_calls"] == 0
    assert receipt["stderr_included"] is False
    assert "PRIVATE OUTPUT" not in json.dumps(receipt, sort_keys=True)
    assert len(calls) == 2


def test_reflector_config_probe_rejects_agents_enabled_without_model(
    tmp_path: Path,
) -> None:
    fake_binary = tmp_path / "codex"
    fake_binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_binary.chmod(0o700)
    invalid = tuple(_reflector_hardening_arguments()) + (
        "--config",
        "agents.enabled=false",
    )

    receipt = inspect_reflector_codex_policy_v2(
        fake_binary,
        probe_runner=_passing_policy_probe,
        hardening_arguments=invalid,
    )

    assert receipt["status"] == "BLOCKED"
    assert receipt["model_calls"] == 0
    assert "REFLECTOR_CODEX_CONFIG_AGENTS_ENABLED_FORBIDDEN" in receipt["finding_codes"]
    assert "REFLECTOR_CODEX_CONFIG_POLICY_INVALID" in receipt["finding_codes"]


def test_reflector_preflight_fails_closed_when_config_parser_rejects(
    tmp_path: Path,
) -> None:
    boundary, audit, _temporary = _boundary(
        tmp_path,
        event={"type": "turn.completed", "usage": {}},
    )

    def rejecting_probe(
        arguments,
        _cwd: Path,
        _environment,
        _timeout_seconds: float,
    ) -> tuple[int, str]:
        if tuple(arguments)[1:] == ("--version",):
            return 0, "codex-cli 0.145.0\n"
        return 1, "PRIVATE STDERR MUST NOT SURFACE"

    boundary.config_probe_runner = rejecting_probe
    with pytest.raises(
        RuntimeError,
        match="REFLECTOR_CODEX_CONFIG_PROBE_REJECTED",
    ):
        boundary.preflight()
    assert not audit.exists()


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
    assert "--clearenv" not in command
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
    assert completed.stdout.strip() == "codex-cli 0.145.0"


def test_reflector_codex_receives_only_sanitized_transport_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy_sentinel = "http://proxy-sentinel.invalid:8080"
    private_sentinel = "private-environment-sentinel"
    proxy_keys = (
        "ALL_PROXY",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "all_proxy",
        "https_proxy",
        "http_proxy",
        "no_proxy",
    )
    for key in proxy_keys:
        monkeypatch.setenv(key, f"{proxy_sentinel}/{key}")
    ca_content = (
        b"-----BEGIN CERTIFICATE-----\nZmFrZS1jZXJ0aWZpY2F0ZQ==\n-----END CERTIFICATE-----\n"
    )
    ca_sources: dict[str, Path] = {}
    for key in (
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
    ):
        source = tmp_path / f"{key}.pem"
        source.write_bytes(ca_content)
        source.chmod(0o600)
        ca_sources[key] = source
        monkeypatch.setenv(key, os.fspath(source))
    ca_directory = tmp_path / "ca-directory"
    ca_directory.mkdir(mode=0o700)
    (ca_directory / "01234567.0").write_bytes(ca_content)
    (ca_directory / "01234567.0").chmod(0o600)
    monkeypatch.setenv("SSL_CERT_DIR", os.fspath(ca_directory))
    monkeypatch.setenv("CHEMBENCH_PRIVATE_SENTINEL", private_sentinel)
    layout_root = tmp_path / "layout"
    layout_root.mkdir()
    layout = _create_layout(layout_root)
    (layout["inputs"] / "dev_loo_dataset.jsonl").write_text("{}\n", encoding="utf-8")
    fake = tmp_path / "fake-codex"
    fake.write_text(
        """#!/bin/sh
set -eu
for key in ALL_PROXY HTTPS_PROXY HTTP_PROXY NO_PROXY all_proxy https_proxy http_proxy no_proxy SSL_CERT_DIR SSL_CERT_FILE REQUESTS_CA_BUNDLE CURL_CA_BUNDLE NODE_EXTRA_CA_CERTS; do
  eval "value=\\${$key:-}"
  [ -n "$value" ]
done
[ -r "$SSL_CERT_FILE" ]
[ -r "$REQUESTS_CA_BUNDLE" ]
[ -r "$CURL_CA_BUNDLE" ]
[ -r "$NODE_EXTRA_CA_CERTS" ]
[ -r "$SSL_CERT_DIR/01234567.0" ]
[ -z "${CHEMBENCH_PRIVATE_SENTINEL:-}" ]
printf 'transport-ok'
""",
        encoding="utf-8",
    )
    fake.chmod(0o700)
    command = _bubblewrap_base_command(
        bwrap_binary=Path(shutil.which("bwrap") or ""),
        layout=layout,
        executable_source=fake,
        executable_command=("/opt/codex-bin",),
        include_codex=True,
    )
    environment = _materialize_reflector_transport_environment(layout)
    assert set(environment) == {
        *proxy_keys,
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
    }
    assert environment["https_proxy"] == f"{proxy_sentinel}/https_proxy"
    assert "CHEMBENCH_PRIVATE_SENTINEL" not in environment
    for key in ca_sources:
        assert environment[key] == f"/transport-ca/{key}.pem"
    assert environment["SSL_CERT_DIR"] == "/transport-ca/SSL_CERT_DIR"
    assert "--clearenv" not in command
    assert proxy_sentinel not in command
    assert private_sentinel not in command
    assert all(os.fspath(source) not in command for source in ca_sources.values())
    assert os.fspath(ca_directory) not in command

    completed = _run_process_group(
        command,
        input_text="",
        timeout_seconds=10.0,
        environment=environment,
    )

    assert completed.returncode == 0
    assert completed.stdout == "transport-ok"
    assert private_sentinel not in completed.stdout
    assert proxy_sentinel not in completed.stderr
    assert private_sentinel not in completed.stderr


def test_reflector_helper_probe_keeps_clearenv(tmp_path: Path) -> None:
    layout_root = tmp_path / "layout"
    layout_root.mkdir()
    layout = _create_layout(layout_root)
    (layout["inputs"] / "dev_loo_dataset.jsonl").write_text("{}\n", encoding="utf-8")
    command = _bubblewrap_base_command(
        bwrap_binary=Path(shutil.which("bwrap") or ""),
        layout=layout,
        executable_source=Path("/bin/true"),
        executable_command=("/bin/true",),
        include_codex=False,
    )

    assert command.count("--clearenv") == 1


def test_reflector_process_rejects_non_transport_environment() -> None:
    with pytest.raises(
        ReflectorBoundaryError,
        match="REFLECTOR_MODEL_TRANSPORT_ENV_INVALID",
    ):
        _run_process_group(
            ("/bin/true",),
            input_text="",
            timeout_seconds=10.0,
            environment={
                "PRIVATE_SECRET": "forbidden",
            },
        )


@pytest.mark.skipif(os.name != "posix", reason="process-group test requires POSIX")
def test_reflector_decode_failure_still_terminates_process_group(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "background.pid"
    executable = tmp_path / "invalid-utf8"
    executable.write_text(
        (
            "#!/usr/bin/env python3\n"
            "import os\n"
            "import subprocess\n"
            "import sys\n"
            f"pid_file = {os.fspath(pid_file)!r}\n"
            "child = subprocess.Popen(\n"
            "    ['/bin/sleep', '60'],\n"
            "    stdin=subprocess.DEVNULL,\n"
            "    stdout=subprocess.DEVNULL,\n"
            "    stderr=subprocess.DEVNULL,\n"
            ")\n"
            "with open(pid_file, 'w', encoding='utf-8') as stream:\n"
            "    stream.write(str(child.pid))\n"
            "os.write(sys.stdout.fileno(), b'\\xff')\n"
        ),
        encoding="utf-8",
    )
    executable.chmod(0o700)

    with pytest.raises(
        ReflectorBoundaryError,
        match="REFLECTOR_PROCESS_IO_FAILED",
    ):
        _run_process_group(
            (os.fspath(executable),),
            input_text="",
            timeout_seconds=10.0,
            environment={},
        )

    background_pid = int(pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 2.0
    while Path(f"/proc/{background_pid}").exists() and time.monotonic() < deadline:
        time.sleep(0.025)
    assert not Path(f"/proc/{background_pid}").exists()


def test_reflector_transport_environment_ignores_empty_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in (
        "ALL_PROXY",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "all_proxy",
        "https_proxy",
        "http_proxy",
        "no_proxy",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
    ):
        monkeypatch.setenv(key, "")
    layout_root = tmp_path / "layout"
    layout_root.mkdir()
    layout = _create_layout(layout_root)

    assert _materialize_reflector_transport_environment(layout) == {}


@pytest.mark.parametrize(
    "ca_key",
    (
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
    ),
)
def test_reflector_rejects_unsafe_ca_file_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ca_key: str,
) -> None:
    real = tmp_path / "ca.pem"
    real.write_text(
        "-----BEGIN CERTIFICATE-----\nZmFrZQ==\n-----END CERTIFICATE-----\n",
        encoding="utf-8",
    )
    real.chmod(0o600)
    link = tmp_path / "ca-link.pem"
    link.symlink_to(real)
    monkeypatch.setenv(ca_key, os.fspath(link))
    layout_root = tmp_path / "layout"
    layout_root.mkdir()
    layout = _create_layout(layout_root)

    with pytest.raises(
        ReflectorBoundaryError,
        match="REFLECTOR_MODEL_TRANSPORT_ENV_INVALID",
    ):
        _materialize_reflector_transport_environment(layout)


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


def test_nested_tool_schema_inside_agent_message_fails_closed(
    tmp_path: Path,
) -> None:
    boundary, audit, temporary_parent = _boundary(
        tmp_path,
        event={
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "safe",
                "tool_calls": [{"type": "file_read"}],
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
    receipt = activation.load_receipt_for_audit()
    assert receipt.status is ReflectorBoundaryStatusV2.SECURITY_TOOL_USE_VIOLATION
    assert dict(receipt.event_counts) == {"file_read": 1, "unknown_tool": 1}
    assert not (upstream / "last-message.md").exists()
    assert stat.S_IMODE((audit / receipt.private_event_reference).stat().st_mode) == 0o600
    assert not list(temporary_parent.glob("openevo-chembench-reflector-v2-*"))


def test_completed_receipt_revalidates_private_event_mode_and_digest(
    tmp_path: Path,
) -> None:
    boundary, audit, _temporary_parent = _boundary(
        tmp_path,
        event={
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "safe"},
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
    assert completed.returncode == 0
    receipt = activation.require_receipt()
    event_path = audit / receipt.private_event_reference

    original = event_path.read_bytes()
    event_path.write_bytes(original + b"\n")
    event_path.chmod(0o600)
    with pytest.raises(RuntimeError, match="REFLECTOR_WRAPPER_RECEIPT_BINDING_INVALID"):
        activation.require_receipt()

    event_path.write_bytes(original)
    event_path.chmod(0o644)
    with pytest.raises(RuntimeError, match="REFLECTOR_WRAPPER_RECEIPT_BINDING_INVALID"):
        activation.require_receipt()


def test_zero_exit_empty_event_stream_is_rejected(
    tmp_path: Path,
) -> None:
    boundary, _audit, _temporary_parent = _boundary(
        tmp_path,
        event="",
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
    assert receipt.status is ReflectorBoundaryStatusV2.INVALID_INVOCATION


def test_codex_failure_receipt_keeps_only_safe_private_diagnostics(
    tmp_path: Path,
) -> None:
    boundary, audit, temporary_parent = _boundary(
        tmp_path,
        event={"type": "turn.completed", "usage": {}},
    )
    expected_stderr = _fake_failing_codex(boundary.real_codex_binary)
    upstream = tmp_path / "upstream"
    upstream.mkdir()

    with boundary.activate() as activation:
        completed = subprocess.run(
            _upstream_args(upstream),
            input="synthetic prompt that must not enter diagnostics",
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )

    assert completed.returncode != 0
    assert completed.stdout == ""
    assert completed.stderr.strip() == "CODEX_FAILED"
    assert not (upstream / "last-message.md").exists()
    receipt = activation.load_receipt_for_audit()
    assert receipt.status is ReflectorBoundaryStatusV2.CODEX_FAILED
    assert receipt.codex_returncode == 1
    assert receipt.stderr_sha256 == hashlib.sha256(expected_stderr.encode("utf-8")).hexdigest()
    assert receipt.stderr_tail_codes == ("CODEX_CONFIG_PARSE_ERROR",)
    serialized = json.dumps(receipt.to_payload(), sort_keys=True)
    assert "synthetic prompt" not in serialized
    assert "invalid type" not in serialized
    assert "boolean false" not in serialized
    assert stat.S_IMODE(activation.receipt_path.stat().st_mode) == 0o600
    assert stat.S_IMODE((audit / receipt.private_event_reference).stat().st_mode) == 0o600
    assert not list(temporary_parent.glob("openevo-chembench-reflector-v2-*"))


def test_legacy_fix1_v2_receipt_remains_readable() -> None:
    receipt = ReflectorExecutionReceiptV2(
        invocation_id="a" * 32,
        status=ReflectorBoundaryStatusV2.CODEX_FAILED,
        mechanism="bubblewrap",
        wrapper_invoked=True,
        real_codex_sha256="1" * 64,
        dev_artifact_sha256="2" * 64,
        ordered_records_sha256="3" * 64,
        record_count=1,
        event_stream_sha256=hashlib.sha256(b"").hexdigest(),
        event_counts=(),
        private_event_reference=f"{'a' * 32}/events.jsonl",
        last_message_sha256=None,
        cleanup_complete=True,
        retry_allowed=False,
        resume_allowed=False,
        replacement_completion_allowed=False,
    )
    legacy = receipt.to_payload()
    legacy["schema_version"] = "chembench4k_reflector_execution_receipt_v2"
    for key in (
        "codex_returncode",
        "source_split",
        "stderr_sha256",
        "stderr_tail_codes",
        "projected_prompt_sha256",
    ):
        legacy.pop(key)

    loaded = ReflectorExecutionReceiptV2.from_payload(legacy)

    assert loaded.status is ReflectorBoundaryStatusV2.CODEX_FAILED
    assert loaded.codex_returncode is None
    assert loaded.stderr_sha256 == hashlib.sha256(b"").hexdigest()
    assert loaded.stderr_tail_codes == ()


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
        config_probe_runner=_passing_policy_probe,
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
    assert receipt.protocol_id == "taskwise_online_evolution_v1"
    assert receipt.source_split == TASKWISE_SOURCE_SPLIT
    assert (
        receipt.projected_prompt_sha256
        == hashlib.sha256(
            _project_reflector_prompt(
                "synthetic taskwise prompt",
                source_split=TASKWISE_SOURCE_SPLIT,
            ).encode("utf-8")
        ).hexdigest()
    )


def test_taskwise_prompt_projection_is_deterministic_and_seals_operational_ids() -> None:
    prompt = """Update text memory.
- job_id: job_aabbccddeeff0011
- dataset_artifact_ids: art_aabbccddeeff0011
Prior: art_1122334455667788 from ds_1122334455667788
Keep the general rules.
"""

    first = _project_reflector_prompt(prompt, source_split=TASKWISE_SOURCE_SPLIT)
    second = _project_reflector_prompt(prompt, source_split=TASKWISE_SOURCE_SPLIT)

    assert first == second
    assert "job_aabbccddeeff0011" not in first
    assert "art_aabbccddeeff0011" not in first
    assert "art_1122334455667788" not in first
    assert "ds_1122334455667788" not in first
    assert "Keep the general rules." in first
    assert "first non-empty line must be exactly `# General Chemistry Memory`" in first
    assert _project_reflector_prompt(prompt, source_split="dev") == prompt


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
