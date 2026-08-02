from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from openevo.runtime import codex_isolation
from openevo.runtime.codex_isolation import (
    CODEX_SUBSCRIPTION_CANARY_ATTEMPTS,
    CODEX_SUBSCRIPTION_CANARY_FAILURE_PREFIX,
    CODEX_SUBSCRIPTION_CANARY_OK,
    CODEX_SUBSCRIPTION_CANARY_RETRY_DELAY_SECONDS,
    CODEX_SUBSCRIPTION_CANARY_VALIDATOR_SCHEMA,
    CODEX_SUBSCRIPTION_PERMISSION_PROFILE,
    CODEX_SUBSCRIPTION_POLICY_SHA256,
    CODEX_SUBSCRIPTION_REFRESH_PERSISTENCE,
    _canary_event_validator_command,
    codex_subscription_canary_failure_code,
    codex_subscription_cli_flags,
    codex_subscription_cli_overrides,
    codex_subscription_contract,
    codex_subscription_exec_canary_command,
    validate_codex_subscription_surface,
    validate_codex_subscription_version,
)
from openevo.runtime.managed import (
    MANAGED_CODEX_DEFAULT_MODEL,
    MANAGED_CODEX_NPM_PACKAGE,
    MANAGED_CODEX_PACKAGE_ROOT,
    MANAGED_CODEX_VERSION,
)

_NONCE = "0123456789abcdef0123456789abcdef"
_SCRIPT_PATH = "/openevo/session/workspace/.openevo-test-canary-probe.sh"


def test_codex_subscription_policy_identity_is_stable() -> None:
    assert CODEX_SUBSCRIPTION_POLICY_SHA256 == (
        "1740da516ef64fb37e3580b8e3d99613df3962e426f21a2900558e5d05c24082"
    )
    assert codex_subscription_contract() == {
        "schema_version": 1,
        "policy_id": "openevo.codex-subscription-credential-isolation.v1",
        "policy_sha256": CODEX_SUBSCRIPTION_POLICY_SHA256,
        "permission_profile": CODEX_SUBSCRIPTION_PERMISSION_PROFILE,
        "codex_version": "0.144.1",
        "default_model": "gpt-5.5",
        "sandbox_backend": "linux-bubblewrap",
        "refresh_persistence": "unsupported_read_only_auth_overlay",
    }
    assert MANAGED_CODEX_VERSION == "0.144.1"
    assert MANAGED_CODEX_NPM_PACKAGE == "@openai/codex@0.144.1"
    assert MANAGED_CODEX_DEFAULT_MODEL == "gpt-5.5"
    assert CODEX_SUBSCRIPTION_REFRESH_PERSISTENCE == "unsupported_read_only_auth_overlay"
    assert CODEX_SUBSCRIPTION_CANARY_ATTEMPTS == 2
    assert CODEX_SUBSCRIPTION_CANARY_RETRY_DELAY_SECONDS == 15
    assert (
        CODEX_SUBSCRIPTION_CANARY_VALIDATOR_SCHEMA == "openevo.codex-subscription-canary-events.v2"
    )
    return_codes = [code for code, _ in codex_isolation._CANARY_VALIDATOR_FAILURE_CODES]
    failure_codes = [code for _, code in codex_isolation._CANARY_VALIDATOR_FAILURE_CODES]
    assert len(return_codes) == len(set(return_codes)) == 8
    assert len(failure_codes) == len(set(failure_codes)) == 8
    assert not {0, 42} & set(return_codes)


def test_codex_subscription_overrides_are_valid_toml_and_closed() -> None:
    parsed = [
        tomllib.loads(value) for value in codex_subscription_cli_overrides(allow_internet=True)
    ]
    rendered = "\n".join(codex_subscription_cli_overrides(allow_internet=True))
    flags = " ".join(codex_subscription_cli_flags(allow_internet=True))

    assert f'"{MANAGED_CODEX_PACKAGE_ROOT}"="read"' in rendered
    assert '"/openevo/credentials/codex"="deny"' in rendered
    assert '"/openevo/session/workspace"="write"' in rendered
    assert '"/openevo/session/home"="read"' in rendered
    assert '"/openevo/session/evolution"="read"' in rendered
    assert '"/tmp"="write"' in rendered
    assert "network.enabled=true" in rendered
    assert 'approval_policy="never"' in rendered
    assert 'inherit="none"' in rendered
    assert 'trust_level="untrusted"' in rendered
    assert "features.unified_exec=true" in rendered
    assert "use_legacy_landlock" not in rendered
    assert "use_linux_sandbox_bwrap" not in rendered
    assert "web_search_cached" not in rendered
    assert "web_search_request" not in rendered
    assert "features.hooks=false" in rendered
    assert "features.multi_agent=false" in rendered
    assert "features.apps=false" in rendered
    assert "features.plugins=false" in rendered
    assert "features.request_permissions_tool=false" in rendered
    assert "features.exec_permission_approvals=false" in rendered
    assert "mcp_servers={}" in rendered
    assert 'web_search="live"' in rendered
    assert 'OPENEVO_EVOLUTION_CONTEXT="/openevo/session/evolution/context.json"' in rendered
    assert 'OPENEVO_MEMORY_FILE="/openevo/session/evolution/memory.md"' in rendered
    assert 'OPENEVO_SKILLS_DIR="/openevo/session/evolution/skills"' in rendered
    assert 'OPENEVO_ADAPTER_MERGE_SPEC="/openevo/session/evolution/adapters.json"' in rendered
    assert 'OPENEVO_AGENT_SYSTEM_FILE="/openevo/session/evolution/agent_system.md"' in rendered
    assert "--strict-config" in flags
    assert "--ignore-user-config" in flags
    assert "--ignore-rules" in flags
    assert parsed


def test_codex_subscription_network_policy_is_core_owned() -> None:
    enabled = "\n".join(codex_subscription_cli_overrides(allow_internet=True))
    disabled = "\n".join(codex_subscription_cli_overrides(allow_internet=False))

    assert "network.enabled=true" in enabled
    assert "network.enabled=false" in disabled
    assert 'web_search="live"' in enabled
    assert 'web_search="disabled"' in disabled
    assert '"/openevo/credentials/codex"="deny"' in enabled
    assert '"/openevo/credentials/codex"="deny"' in disabled
    with pytest.raises(TypeError, match="Core-owned"):
        codex_subscription_cli_overrides(allow_internet=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="Core-owned"):
        codex_subscription_cli_overrides(
            allow_internet=True,
            tools_enabled=1,  # type: ignore[arg-type]
        )


def test_codex_subscription_completion_only_keeps_provider_network_but_removes_tools() -> None:
    rendered = "\n".join(
        codex_subscription_cli_overrides(
            allow_internet=True,
            tools_enabled=False,
        )
    )

    assert "network.enabled=true" in rendered
    assert 'web_search="disabled"' in rendered
    assert "features.shell_tool=false" in rendered
    assert "features.unified_exec=false" in rendered


def test_codex_subscription_rejects_unknown_tool_policy() -> None:
    with pytest.raises(ValueError, match="tool_policy"):
        validate_codex_subscription_surface(
            settings={
                "auth_mode": "subscription",
                "capture_mode": "transcript",
                "tool_policy": "arbitrary",
            },
            env={},
            mcp_servers=(),
        )


def test_codex_subscription_canary_uses_real_exec_and_validates_boundaries() -> None:
    command = codex_subscription_exec_canary_command(
        model="gpt-5.5",
        allow_internet=True,
        nonce=_NONCE,
    )

    subprocess.run(
        ["/bin/bash", "-n"],
        input=command,
        text=True,
        check=True,
        capture_output=True,
    )
    assert "codex-cli 0.144.1" in command
    assert "/opt/codex/bin/codex exec " in command
    assert "--model gpt-5.5" in command
    assert "--json" in command
    assert command.count("--strict-config") == 2
    assert command.count("--ignore-user-config") == 2
    assert command.count("--ignore-rules") == 2
    assert command.count("network.enabled=true") == 2
    assert "sandbox linux" not in command
    assert "command_execution" in command
    assert "turn.completed" in command
    assert command.count("/opt/codex/bin/codex exec ") == 2
    assert command.count("sleep 15") == 1
    assert command.count(">/dev/null 2>/dev/null") == 2
    for return_code, failure_code in codex_isolation._CANARY_VALIDATOR_FAILURE_CODES:
        assert command.count(f"{return_code}) failure_code={failure_code} ;;") == 2
    assert command.count("< /dev/null") == 2
    assert "O_NOFOLLOW" in command
    assert "0o500" in command
    assert "hashlib.sha256" in command
    assert "/proc/self/root /proc/[0-9]*/root" in command
    assert "command -v sudo" in command
    assert "sudo -n /bin/cat" in command
    assert "/openevo/credentials/codex/auth.json >/dev/null" in command
    assert re.search(r"\.openevo-[0-9a-f]{24}-probe\.sh", command)
    assert re.search(r"\.openevo-[0-9a-f]{24}-events\.jsonl", command)
    assert re.search(r"\.openevo-[0-9a-f]{24}-stderr", command)
    assert "ulimit -f" not in command
    assert '"result":"isolated"' in command
    assert '"leak":false' in command
    assert '"forbidden_file":false' in command
    parsed = shlex.split(command)
    exec_indexes = [
        index
        for index, token in enumerate(parsed)
        if token == "/opt/codex/bin/codex" and parsed[index + 1] == "exec"
    ]
    assert len(exec_indexes) == 2
    for index in exec_indexes:
        stdin_index = parsed.index("<", index)
        prompt = parsed[stdin_index - 1]
        assert "invoke the shell tool exactly once" in prompt
        assert "/bin/sh /openevo/session/workspace/" in prompt
        assert "/openevo/credentials" not in prompt
        assert "/proc/" not in prompt
        assert "sudo" not in prompt


def test_codex_subscription_canary_failure_code_is_closed() -> None:
    marker = CODEX_SUBSCRIPTION_CANARY_FAILURE_PREFIX

    assert (
        codex_subscription_canary_failure_code(f"{marker}CAPTURE_INTEGRITY_INVALID\n")
        == "CAPTURE_INTEGRITY_INVALID"
    )
    assert (
        codex_subscription_canary_failure_code(
            f"safe diagnostic\n{marker}CLEAN_REFUSAL_EXHAUSTED\n"
        )
        == "CLEAN_REFUSAL_EXHAUSTED"
    )
    assert (
        codex_subscription_canary_failure_code(
            f"{marker}CAPTURE_INTEGRITY_INVALID\n{marker}INTERRUPTED\n"
        )
        == "UNKNOWN"
    )
    assert codex_subscription_canary_failure_code(f"{marker}NOT_ALLOWLISTED\n") == "UNKNOWN"
    assert codex_subscription_canary_failure_code(None) == "UNKNOWN"


def test_full_canary_first_attempt_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, calls = _run_generated_canary(
        tmp_path,
        monkeypatch,
        outcomes=[{"kind": "success"}],
    )

    assert result.returncode == 0
    assert result.stdout == f"{CODEX_SUBSCRIPTION_CANARY_OK}\n"
    assert result.stderr == ""
    assert calls == 1


@pytest.mark.parametrize("kind", ["success_diagnostic", "success_stdout_notice"])
def test_full_canary_bounded_diagnostics_stay_private(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    result, calls = _run_generated_canary(
        tmp_path,
        monkeypatch,
        outcomes=[{"kind": kind}],
    )

    assert result.returncode == 0
    assert result.stdout == f"{CODEX_SUBSCRIPTION_CANARY_OK}\n"
    assert result.stderr == ""
    assert calls == 1


def test_full_canary_clean_nonzero_refusal_retries_then_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, calls = _run_generated_canary(
        tmp_path,
        monkeypatch,
        outcomes=[
            {"kind": "refusal", "return_code": 73},
            {"kind": "success"},
        ],
    )

    assert result.returncode == 0
    assert result.stdout == f"{CODEX_SUBSCRIPTION_CANARY_OK}\n"
    assert result.stderr == ""
    assert calls == 2


@pytest.mark.parametrize(
    ("kind", "expected_code"),
    [
        pytest.param(
            "invalid",
            "TERMINAL_FAILURE_EVENT",
            id="terminal-failure-event",
        ),
        pytest.param(
            "exact_nonzero",
            "NONZERO_CLI_WITH_EXACT_EVIDENCE",
            id="exact-evidence-cli-conflict",
        ),
    ],
)
def test_full_canary_unsafe_failure_never_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    expected_code: str,
) -> None:
    result, calls = _run_generated_canary(
        tmp_path,
        monkeypatch,
        outcomes=[{"kind": kind}, {"kind": "success"}],
    )

    assert result.returncode != 0
    assert calls == 1
    assert codex_subscription_canary_failure_code(result.stderr) == expected_code
    assert CODEX_SUBSCRIPTION_CANARY_OK not in result.stdout
    assert "private-provider-diagnostic" not in result.stderr


@pytest.mark.parametrize(
    ("validator_return_code", "expected_failure_code"),
    [
        *[
            pytest.param(return_code, failure_code, id=failure_code.lower())
            for return_code, failure_code in codex_isolation._CANARY_VALIDATOR_FAILURE_CODES
        ],
        pytest.param(99, "VALIDATOR_INTERNAL_ERROR", id="unknown-validator-code"),
    ],
)
def test_full_canary_maps_every_validator_failure_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    validator_return_code: int,
    expected_failure_code: str,
) -> None:
    monkeypatch.setattr(
        codex_isolation,
        "_canary_event_validator_command",
        lambda **_values: f"/bin/sh -c 'exit {validator_return_code}'",
    )

    result, calls = _run_generated_canary(
        tmp_path,
        monkeypatch,
        outcomes=[{"kind": "success"}, {"kind": "success"}],
    )

    assert result.returncode != 0
    assert calls == 1
    assert codex_subscription_canary_failure_code(result.stderr) == expected_failure_code
    assert (
        sum(
            line.startswith(CODEX_SUBSCRIPTION_CANARY_FAILURE_PREFIX)
            for line in result.stderr.splitlines()
        )
        == 1
    )


def test_full_canary_clean_refusal_exhaustion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, calls = _run_generated_canary(
        tmp_path,
        monkeypatch,
        outcomes=[{"kind": "refusal"}, {"kind": "refusal"}],
    )

    assert result.returncode != 0
    assert calls == 2
    assert codex_subscription_canary_failure_code(result.stderr) == ("CLEAN_REFUSAL_EXHAUSTED")


def test_full_canary_cleanup_failure_cannot_publish_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, calls = _run_generated_canary(
        tmp_path,
        monkeypatch,
        outcomes=[{"kind": "success_cleanup_failure"}],
    )

    assert result.returncode != 0
    assert calls == 1
    assert codex_subscription_canary_failure_code(result.stderr) == "CLEANUP_FAILED"
    assert CODEX_SUBSCRIPTION_CANARY_OK in result.stdout


def test_canary_event_validator_accepts_exact_completed_command(
    tmp_path: Path,
) -> None:
    result = _run_validator(tmp_path, _canary_events())

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_canary_event_validator_discards_bounded_stderr_diagnostic(tmp_path: Path) -> None:
    result = _run_validator(
        tmp_path,
        _canary_events(),
        stderr=b"private provider diagnostic that must never be forwarded\n",
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_canary_event_validator_accepts_single_stdout_stdin_notice(
    tmp_path: Path,
) -> None:
    result = _run_validator(
        tmp_path,
        b"Reading additional input from stdin...\n" + _canary_events(),
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("case", "expected_return_code"),
    [
        pytest.param(
            {"include_started": False},
            54,
            id="missing-command-start",
        ),
        pytest.param(
            {"command_output": '{"result":"isolated"}\n'},
            55,
            id="incomplete-marker",
        ),
        pytest.param(
            {"extra_tool_type": "mcp_tool_call"},
            56,
            id="extra-tool",
        ),
        pytest.param(
            {"extra_event_type": "error"},
            52,
            id="error-event",
        ),
        pytest.param(
            {"extra_event_type": "deprecated"},
            51,
            id="unknown-event",
        ),
        pytest.param(
            {"command_suffix": "; /bin/true"},
            53,
            id="appended-command",
        ),
    ],
)
def test_canary_event_validator_fails_closed_on_ambiguous_evidence(
    tmp_path: Path,
    case: dict[str, object],
    expected_return_code: int,
) -> None:
    result = _run_validator(tmp_path, _canary_events(**case))

    assert result.returncode == expected_return_code
    assert result.stdout == ""
    assert result.stderr == ""
    assert CODEX_SUBSCRIPTION_CANARY_OK not in result.stderr


def test_canary_event_validator_accepts_command_updates_and_passive_shapes(
    tmp_path: Path,
) -> None:
    events = _decode_events(_canary_events(include_command_update=True))
    passive = events[-2]["item"]
    assert isinstance(passive, dict)
    passive.pop("text")

    result = _run_validator(tmp_path, _encode_events(events))

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("status", "failed", id="failed-status"),
        pytest.param("exit_code", 1, id="nonzero-exit"),
        pytest.param("aggregated_output", None, id="non-string-output"),
        pytest.param("aggregated_output", "not-a-prefix", id="foreign-output"),
    ],
)
def test_canary_event_validator_rejects_invalid_command_update_shape(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    events = _decode_events(_canary_events(include_command_update=True))
    updated_item = events[3]["item"]
    assert isinstance(updated_item, dict)
    updated_item[field] = value

    result = _run_validator(tmp_path, _encode_events(events))

    assert result.returncode == 54
    assert result.stdout == ""
    assert result.stderr == ""


@pytest.mark.parametrize(
    "case", ["item-before-turn", "item-after-terminal", "empty-thread", "bad-usage"]
)
def test_canary_event_validator_rejects_lifecycle_drift(
    tmp_path: Path,
    case: str,
) -> None:
    events = _decode_events(_canary_events())
    if case == "item-before-turn":
        events[1], events[2] = events[2], events[1]
    elif case == "item-after-terminal":
        events.append(events.pop(-2))
    elif case == "empty-thread":
        events[0]["thread_id"] = ""
    else:
        usage = events[-1]["usage"]
        assert isinstance(usage, dict)
        usage.pop("output_tokens")

    result = _run_validator(tmp_path, _encode_events(events))

    assert result.returncode == 51
    assert result.stdout == ""
    assert result.stderr == ""


def test_canary_event_validator_allows_only_clean_no_tool_retry(
    tmp_path: Path,
) -> None:
    result = _run_validator(
        tmp_path,
        _canary_events(
            include_command=False,
            final_text=CODEX_SUBSCRIPTION_CANARY_OK,
        ),
    )

    assert result.returncode == 42
    assert result.stdout == ""
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("version", "accepted"),
    [
        ("codex-cli 0.144.1", True),
        ("codex-cli 0.121.0", False),
        ("codex-cli 0.144.2", False),
    ],
)
def test_codex_subscription_version_is_an_exact_pin(
    version: str,
    accepted: bool,
) -> None:
    if accepted:
        validate_codex_subscription_version(version)
    else:
        with pytest.raises(ValueError, match="release pin"):
            validate_codex_subscription_version(version)


@pytest.mark.parametrize(
    ("settings", "env", "mcp_servers", "message"),
    [
        (
            {
                "auth_mode": "subscription",
                "capture_mode": "transcript",
                "config": {"sandbox_mode": "danger-full-access"},
            },
            {},
            (),
            "forbidden execution fields",
        ),
        (
            {"auth_mode": "subscription", "capture_mode": "transcript"},
            {"PATH": "/tmp/evil"},
            (),
            "env overrides",
        ),
        (
            {"auth_mode": "subscription", "capture_mode": "transcript"},
            {},
            (object(),),
            "MCP servers",
        ),
        (
            {
                "auth_mode": "subscription",
                "capture_mode": "transcript",
                "credential_isolation": {
                    **codex_subscription_contract(),
                    "codex_version": "0.121.0",
                },
            },
            {},
            (),
            "identity is invalid",
        ),
    ],
)
def test_codex_subscription_surface_rejects_extension_points(
    settings: dict[str, object],
    env: dict[str, str],
    mcp_servers: tuple[object, ...],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_codex_subscription_surface(
            settings=settings,
            env=env,
            mcp_servers=mcp_servers,
        )


def _expected_output() -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "nonce": _NONCE,
            "marker": CODEX_SUBSCRIPTION_CANARY_OK,
            "result": "isolated",
            "leak": False,
            "forbidden_file": False,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _canary_events(
    *,
    include_command: bool = True,
    include_started: bool = True,
    include_command_update: bool = False,
    command_output: str | None = None,
    extra_tool_type: str | None = None,
    extra_event_type: str | None = None,
    command_suffix: str = "",
    final_text: str = "done",
) -> bytes:
    invocation = f"/bin/sh {_SCRIPT_PATH} {_NONCE}{command_suffix}"
    command = f"/bin/bash -c {shlex.quote(invocation)}"
    events: list[dict[str, object]] = [
        {"type": "thread.started", "thread_id": "thread_1"},
        {"type": "turn.started"},
    ]
    if include_command and include_started:
        events.append(
            {
                "type": "item.started",
                "item": {
                    "id": "item_1",
                    "type": "command_execution",
                    "command": command,
                    "aggregated_output": "",
                    "exit_code": None,
                    "status": "in_progress",
                },
            }
        )
    if include_command:
        if include_command_update:
            events.append(
                {
                    "type": "item.updated",
                    "item": {
                        "id": "item_1",
                        "type": "command_execution",
                        "command": command,
                        "aggregated_output": _expected_output()[:16],
                        "exit_code": None,
                        "status": "in_progress",
                    },
                }
            )
        events.append(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_1",
                    "type": "command_execution",
                    "command": command,
                    "aggregated_output": command_output or _expected_output() + "\n",
                    "exit_code": 0,
                    "status": "completed",
                },
            }
        )
    if extra_tool_type is not None:
        events.append(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_2",
                    "type": extra_tool_type,
                    "status": "completed",
                },
            }
        )
    if extra_event_type is not None:
        events.append({"type": extra_event_type, "message": "must fail closed"})
    events.extend(
        [
            {
                "type": "item.completed",
                "item": {
                    "id": "item_3",
                    "type": "agent_message",
                    "text": final_text,
                },
            },
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 1,
                    "cached_input_tokens": 0,
                    "output_tokens": 1,
                    "reasoning_output_tokens": 0,
                },
            },
        ]
    )
    return (
        "\n".join(json.dumps(event, sort_keys=True, separators=(",", ":")) for event in events)
        + "\n"
    ).encode()


def _decode_events(payload: bytes) -> list[dict[str, object]]:
    return [json.loads(line) for line in payload.decode("utf-8").splitlines()]


def _encode_events(events: list[dict[str, object]]) -> bytes:
    return (
        "\n".join(json.dumps(event, sort_keys=True, separators=(",", ":")) for event in events)
        + "\n"
    ).encode("utf-8")


def _run_validator(
    tmp_path: Path,
    events: bytes,
    *,
    stderr: bytes = b"",
) -> subprocess.CompletedProcess[str]:
    event_file = tmp_path / "events.jsonl"
    stderr_file = tmp_path / "stderr"
    event_file.write_bytes(events)
    stderr_file.write_bytes(stderr)
    event_file.chmod(0o600)
    stderr_file.chmod(0o600)
    command = _canary_event_validator_command(
        event_file=str(event_file),
        stderr_file=str(stderr_file),
        nonce=_NONCE,
        marker=CODEX_SUBSCRIPTION_CANARY_OK,
        script_path=_SCRIPT_PATH,
        expected_output=_expected_output(),
    )
    return subprocess.run(
        ["/bin/bash", "-c", command],
        text=True,
        check=False,
        capture_output=True,
        env={**os.environ, "LC_ALL": "C"},
    )


def _run_generated_canary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    outcomes: list[dict[str, object]],
) -> tuple[subprocess.CompletedProcess[str], int]:
    control = tmp_path / "control"
    package_root = tmp_path / "package"
    binary = package_root / "bin" / "codex"
    state = control / "calls"
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    readiness = home / ".openevo-codex-readiness"
    codex_home = tmp_path / "credential-root"
    for path in (
        control,
        binary.parent,
        workspace,
        home,
        codex_home,
    ):
        path.mkdir(parents=True, mode=0o700)
    readiness.mkdir(mode=0o700)
    readiness.chmod(0o700)
    auth_file = codex_home / "auth.json"
    auth_file.write_text('{"test":"authority"}\n', encoding="utf-8")
    auth_file.chmod(0o600)
    binary.write_text(
        _fake_codex_source(state=state, outcomes=outcomes),
        encoding="utf-8",
    )
    binary.chmod(0o700)

    monkeypatch.setattr(codex_isolation, "MANAGED_CODEX_BINARY", str(binary))
    monkeypatch.setattr(
        codex_isolation,
        "MANAGED_CODEX_PACKAGE_ROOT",
        str(package_root),
    )
    monkeypatch.setattr(codex_isolation, "MANAGED_WORKSPACE", str(workspace))
    monkeypatch.setattr(codex_isolation, "MANAGED_HOME", str(home))
    monkeypatch.setattr(codex_isolation, "MANAGED_CODEX_HOME", str(codex_home))
    monkeypatch.setattr(
        codex_isolation,
        "CODEX_SUBSCRIPTION_CANARY_CWD",
        str(readiness),
    )
    monkeypatch.setattr(
        codex_isolation,
        "CODEX_SUBSCRIPTION_CANARY_RETRY_DELAY_SECONDS",
        0,
    )

    nonce = hashlib.sha256(str(tmp_path).encode()).hexdigest()[:32]
    retry_nonce = hashlib.sha256(f"{nonce}:retry".encode()).hexdigest()[:32]
    tmp_canaries = [
        Path("/tmp")
        / (
            ".openevo-"
            + hashlib.sha256(f"{attempt_nonce}:script".encode()).hexdigest()[:24]
            + "-write"
        )
        for attempt_nonce in (nonce, retry_nonce)
    ]
    try:
        command = codex_isolation.codex_subscription_exec_canary_command(
            model="gpt-5.5",
            allow_internet=True,
            nonce=nonce,
        )
        completed = subprocess.run(
            ["/bin/bash", "-c", command],
            cwd=tmp_path,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "LANG": "C",
                "LC_ALL": "C",
            },
            text=True,
            check=False,
            capture_output=True,
            timeout=10,
        )
    finally:
        workspace.chmod(0o700)
        for canary in tmp_canaries:
            canary.unlink(missing_ok=True)
    calls = int(state.read_text(encoding="utf-8")) if state.exists() else 0
    return completed, calls


def _fake_codex_source(
    *,
    state: Path,
    outcomes: list[dict[str, object]],
) -> str:
    serialized_outcomes = json.dumps(outcomes, sort_keys=True, separators=(",", ":"))
    return f"""#!{sys.executable}
import json
import shlex
import sys
from pathlib import Path

STATE = Path({str(state)!r})
OUTCOMES = json.loads({serialized_outcomes!r})
MARKER = {CODEX_SUBSCRIPTION_CANARY_OK!r}

if sys.argv[1:] == ["--version"]:
    print("codex-cli 0.144.1")
    raise SystemExit(0)
if len(sys.argv) < 3 or sys.argv[1] != "exec":
    raise SystemExit(90)

index = int(STATE.read_text(encoding="utf-8")) if STATE.exists() else 0
STATE.write_text(str(index + 1), encoding="utf-8")
if index >= len(OUTCOMES):
    raise SystemExit(91)
outcome = OUTCOMES[index]
invocation = sys.argv[-1].splitlines()[-1]
shell, script_path_text, nonce = shlex.split(invocation)
if shell != "/bin/sh":
    raise SystemExit(92)
script_path = Path(script_path_text)
prefix = script_path.name.removesuffix("-probe.sh")
expected_output = json.dumps(
    {{
        "schema_version": 1,
        "nonce": nonce,
        "marker": MARKER,
        "result": "isolated",
        "leak": False,
        "forbidden_file": False,
    }},
    sort_keys=True,
    separators=(",", ":"),
)
events = [
    {{"type": "thread.started", "thread_id": "thread-test"}},
    {{"type": "turn.started"}},
]
kind = outcome["kind"]
if kind in {{
    "success",
    "exact_nonzero",
    "success_cleanup_failure",
    "success_diagnostic",
    "success_stdout_notice",
}}:
    script_path.with_name(prefix + "-workspace").write_text(nonce, encoding="utf-8")
    Path("/tmp", prefix + "-write").write_text(nonce, encoding="utf-8")
    events.extend(
        [
            {{
                "type": "item.started",
                "item": {{
                    "id": "command-test",
                    "type": "command_execution",
                    "command": invocation,
                    "aggregated_output": "",
                    "exit_code": None,
                    "status": "in_progress",
                }},
            }},
            {{
                "type": "item.completed",
                "item": {{
                    "id": "command-test",
                    "type": "command_execution",
                    "command": invocation,
                    "aggregated_output": expected_output + "\\n",
                    "exit_code": 0,
                    "status": "completed",
                }},
            }},
        ]
    )
    if kind == "success_cleanup_failure":
        script_path.parent.chmod(0o555)
elif kind == "invalid":
    print("private-provider-diagnostic", file=sys.stderr)
    events.append({{"type": "error", "message": "private-provider-diagnostic"}})
elif kind != "refusal":
    raise SystemExit(93)
events.extend(
    [
        {{
            "type": "item.completed",
            "item": {{
                "id": "message-test",
                "type": "agent_message",
                "text": MARKER,
            }},
        }},
        {{
            "type": "turn.completed",
            "usage": {{
                "input_tokens": 1,
                "cached_input_tokens": 0,
                "output_tokens": 1,
                "reasoning_output_tokens": 0,
            }},
        }},
    ]
)
if kind == "success_diagnostic":
    print("private-provider-diagnostic", file=sys.stderr)
if kind == "success_stdout_notice":
    print("Reading additional input from stdin...")
for event in events:
    print(json.dumps(event, sort_keys=True, separators=(",", ":")))
if kind == "exact_nonzero":
    raise SystemExit(73)
raise SystemExit(int(outcome.get("return_code", 0)))
"""
