"""Closed Codex subscription credential-isolation policy."""

from __future__ import annotations

import hashlib
import json
import secrets
import shlex
from collections.abc import Mapping, Sequence
from typing import Final

from openevo.runtime.managed import (
    MANAGED_CODEX_BINARY,
    MANAGED_CODEX_DEFAULT_MODEL,
    MANAGED_CODEX_HOME,
    MANAGED_CODEX_PACKAGE_ROOT,
    MANAGED_CODEX_READINESS_WORKSPACE,
    MANAGED_CODEX_VERSION,
    MANAGED_HOME,
    MANAGED_PATH,
    MANAGED_WORKSPACE,
)

CODEX_SUBSCRIPTION_POLICY_ID: Final[str] = "openevo.codex-subscription-credential-isolation.v1"
CODEX_SUBSCRIPTION_PERMISSION_PROFILE: Final[str] = "openevo_codex_subscription_v1"
CODEX_SUBSCRIPTION_CODEX_VERSION: Final[str] = MANAGED_CODEX_VERSION
CODEX_SUBSCRIPTION_SANDBOX_BACKEND: Final[str] = "linux-bubblewrap"
CODEX_SUBSCRIPTION_CONTRACT_KEY: Final[str] = "credential_isolation"
CODEX_SUBSCRIPTION_READINESS_KEY: Final[str] = "credential_isolation_receipt"
CODEX_SUBSCRIPTION_TOOL_POLICY_KEY: Final[str] = "tool_policy"
CODEX_SUBSCRIPTION_TOOL_POLICY_DISABLED: Final[str] = "disabled"
CODEX_SUBSCRIPTION_CANARY_OK: Final[str] = "openevo-codex-subscription-real-exec-ready-v1"
CODEX_SUBSCRIPTION_CANARY_ATTEMPTS: Final[int] = 2
CODEX_SUBSCRIPTION_CANARY_RETRY_DELAY_SECONDS: Final[int] = 15
CODEX_SUBSCRIPTION_CANARY_FAILURE_PREFIX: Final[str] = (
    "openevo-codex-subscription-canary-failure-v1:"
)
CODEX_SUBSCRIPTION_CANARY_VALIDATOR_SCHEMA: Final[str] = (
    "openevo.codex-subscription-canary-events.v2"
)
CODEX_SUBSCRIPTION_REFRESH_PERSISTENCE: Final[str] = "unsupported_read_only_auth_overlay"
CODEX_SUBSCRIPTION_CANARY_CWD: Final[str] = MANAGED_CODEX_READINESS_WORKSPACE
_CANARY_RESULT: Final[str] = "isolated"
_CANARY_NONCE_BYTES: Final[int] = 16
_CANARY_CLEAN_REFUSAL_RC: Final[int] = 42
_CANARY_VALIDATOR_FAILURE_CODES: Final[tuple[tuple[int, str], ...]] = (
    (50, "CAPTURE_INTEGRITY_INVALID"),
    (51, "EVENT_PROTOCOL_INVALID"),
    (52, "TERMINAL_FAILURE_EVENT"),
    (53, "COMMAND_IDENTITY_INVALID"),
    (54, "COMMAND_LIFECYCLE_INVALID"),
    (55, "PROBE_RESULT_INVALID"),
    (56, "UNEXPECTED_TOOL_EVENT"),
    (57, "VALIDATOR_CONTRACT_INVALID"),
)
_CANARY_FAILURE_CODES: Final[frozenset[str]] = frozenset(
    {
        *(code for _, code in _CANARY_VALIDATOR_FAILURE_CODES),
        "CLEAN_REFUSAL_EXHAUSTED",
        "CLEANUP_FAILED",
        "INTERRUPTED",
        "LOCAL_PRECONDITION_FAILED",
        "NONZERO_CLI_WITH_EXACT_EVIDENCE",
        "PROBE_OUTPUT_INVALID",
        "REFUSAL_INVENTORY_INVALID",
        "RETRY_DELAY_INTERRUPTED",
        "SCRIPT_AUTHORITY_INVALID",
        "VALIDATOR_INTERNAL_ERROR",
    }
)
_EVOLUTION_ROOT: Final[str] = "/openevo/session/evolution"
_EVOLUTION_SHELL_ENV: Final[tuple[tuple[str, str], ...]] = (
    ("OPENEVO_EVOLUTION_CONTEXT", f"{_EVOLUTION_ROOT}/context.json"),
    ("OPENEVO_MEMORY_FILE", f"{_EVOLUTION_ROOT}/memory.md"),
    ("OPENEVO_SKILLS_DIR", f"{_EVOLUTION_ROOT}/skills"),
    ("OPENEVO_ADAPTER_MERGE_SPEC", f"{_EVOLUTION_ROOT}/adapters.json"),
    ("OPENEVO_AGENT_SYSTEM_FILE", f"{_EVOLUTION_ROOT}/agent_system.md"),
)

_ALLOWED_SUBSCRIPTION_SETTINGS: Final[frozenset[str]] = frozenset(
    {
        "auth_mode",
        "capture_mode",
        "credential_isolation",
        "native_memory_policy",
        "reasoning_effort",
        "reasoning_summary",
        CODEX_SUBSCRIPTION_TOOL_POLICY_KEY,
    }
)
_FILESYSTEM_POLICY: Final[tuple[tuple[str, str], ...]] = (
    (":minimal", "read"),
    (MANAGED_CODEX_PACKAGE_ROOT, "read"),
    (MANAGED_WORKSPACE, "write"),
    (MANAGED_HOME, "read"),
    (_EVOLUTION_ROOT, "read"),
    (":tmpdir", "write"),
    ("/tmp", "write"),
    (MANAGED_CODEX_HOME, "deny"),
)
_DISABLED_EXECUTION_FEATURES: Final[tuple[str, ...]] = (
    "apps",
    "auth_elicitation",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "code_mode",
    "code_mode_host",
    "code_mode_only",
    "computer_use",
    "deferred_executor",
    "enable_fanout",
    "enable_mcp_apps",
    "exec_permission_approvals",
    "goals",
    "guardian_approval",
    "hooks",
    "image_generation",
    "in_app_browser",
    "multi_agent",
    "multi_agent_v2",
    "network_proxy",
    "plugin_sharing",
    "plugins",
    "remote_plugin",
    "request_permissions_tool",
    "shell_snapshot",
    "shell_zsh_fork",
    "skill_mcp_dependency_install",
    "standalone_web_search",
    "tool_call_mcp_elicitation",
    "tool_suggest",
    "unified_exec_zsh_fork",
    "workspace_dependencies",
)
_POLICY_SPEC: Final[dict[str, object]] = {
    "schema_version": 1,
    "policy_id": CODEX_SUBSCRIPTION_POLICY_ID,
    "permission_profile": CODEX_SUBSCRIPTION_PERMISSION_PROFILE,
    "codex_version": CODEX_SUBSCRIPTION_CODEX_VERSION,
    "default_model": MANAGED_CODEX_DEFAULT_MODEL,
    "sandbox_backend": CODEX_SUBSCRIPTION_SANDBOX_BACKEND,
    "approval_policy": "never",
    "task_network": {
        "owner": "runtime_spec.allow_internet",
        "science_default": True,
    },
    "filesystem": [{"path": path, "access": access} for path, access in _FILESYSTEM_POLICY],
    "shell_environment": {
        "inherit": "none",
        "set": {
            "HOME": MANAGED_HOME,
            "PATH": MANAGED_PATH,
            "TMPDIR": "/tmp",
            **dict(_EVOLUTION_SHELL_ENV),
        },
    },
    "disabled_execution_features": list(_DISABLED_EXECUTION_FEATURES),
    "project_config_trust": "untrusted",
    "credential_parent_view_access": "read-write",
    "credential_auth_overlay_access": "read-only",
    "credential_tool_filesystem_access": "deny",
    "credential_refresh_persistence": CODEX_SUBSCRIPTION_REFRESH_PERSISTENCE,
    "readiness_command": "codex exec",
    "readiness_attempts": CODEX_SUBSCRIPTION_CANARY_ATTEMPTS,
    "readiness_clean_refusal_retry_delay_seconds": (
        CODEX_SUBSCRIPTION_CANARY_RETRY_DELAY_SECONDS
    ),
    "readiness_retry_class": "exact_completed_turn_without_tool",
    "readiness_validator_schema": CODEX_SUBSCRIPTION_CANARY_VALIDATOR_SCHEMA,
    "readiness_validator_failure_codes": [
        {"return_code": return_code, "failure_code": failure_code}
        for return_code, failure_code in _CANARY_VALIDATOR_FAILURE_CODES
    ],
    "readiness_stderr_handling": "bounded_private_discard",
    "readiness_stdout_notice": "single_exact_optional_prefix",
    "readiness_context": {
        "working_directory": CODEX_SUBSCRIPTION_CANARY_CWD,
        "project_instructions": "absent",
        "evolution_skills": "not_installed",
    },
    "readiness_evidence": "completed_command_execution_event",
    "readiness_canaries": [
        "exact_codex_version",
        "parent_auth_read",
        "tool_direct_auth_read_denied",
        "tool_proc_self_root_auth_read_denied",
        "tool_proc_pid_root_auth_read_denied",
        "tool_sudo_auth_read_denied_or_absent",
        "workspace_write",
        "home_read_only",
        "tmp_write",
    ],
}
CODEX_SUBSCRIPTION_POLICY_SHA256: Final[str] = hashlib.sha256(
    json.dumps(
        _POLICY_SPEC,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()


def codex_subscription_contract() -> dict[str, object]:
    """Return the stable data-only policy/profile identity."""

    return {
        "schema_version": 1,
        "policy_id": CODEX_SUBSCRIPTION_POLICY_ID,
        "policy_sha256": CODEX_SUBSCRIPTION_POLICY_SHA256,
        "permission_profile": CODEX_SUBSCRIPTION_PERMISSION_PROFILE,
        "codex_version": CODEX_SUBSCRIPTION_CODEX_VERSION,
        "default_model": MANAGED_CODEX_DEFAULT_MODEL,
        "sandbox_backend": CODEX_SUBSCRIPTION_SANDBOX_BACKEND,
        "refresh_persistence": CODEX_SUBSCRIPTION_REFRESH_PERSISTENCE,
    }


def codex_subscription_readiness_receipt() -> dict[str, object]:
    """Return the receipt published only after the real-exec canary passes."""

    return {
        **codex_subscription_contract(),
        "status": "passed",
        "canary": CODEX_SUBSCRIPTION_CANARY_OK,
        "evidence": "completed_command_execution_event",
    }


def codex_subscription_canary_failure_code(stderr: str | None) -> str:
    """Return one closed failure code without surfacing raw canary output."""

    if not isinstance(stderr, str):
        return "UNKNOWN"
    matches = [
        line.removeprefix(CODEX_SUBSCRIPTION_CANARY_FAILURE_PREFIX)
        for line in stderr.splitlines()
        if line.startswith(CODEX_SUBSCRIPTION_CANARY_FAILURE_PREFIX)
    ]
    if len(matches) != 1 or matches[0] not in _CANARY_FAILURE_CODES:
        return "UNKNOWN"
    return matches[0]


def validate_codex_subscription_surface(
    *,
    settings: Mapping[str, object],
    env: Mapping[str, str],
    mcp_servers: Sequence[object],
) -> None:
    """Reject caller-controlled subscription execution extensions."""

    unknown = sorted(set(settings) - _ALLOWED_SUBSCRIPTION_SETTINGS)
    if unknown:
        names = ", ".join(unknown)
        raise ValueError(
            f"Codex subscription settings contain forbidden execution fields: {names}"
        )
    if env:
        raise ValueError("Codex subscription agent env overrides are forbidden")
    if mcp_servers:
        raise ValueError("Codex subscription MCP servers are forbidden")
    native_memory_policy = settings.get("native_memory_policy")
    if native_memory_policy not in {None, "preserve"}:
        raise ValueError("Codex subscription native_memory_policy must be omitted or 'preserve'")
    supplied = settings.get(CODEX_SUBSCRIPTION_CONTRACT_KEY)
    if supplied is not None and supplied != codex_subscription_contract():
        raise ValueError("Codex subscription credential-isolation identity is invalid")
    tool_policy = settings.get(CODEX_SUBSCRIPTION_TOOL_POLICY_KEY)
    if tool_policy not in {None, CODEX_SUBSCRIPTION_TOOL_POLICY_DISABLED}:
        raise ValueError("Codex subscription tool_policy must be omitted or 'disabled'")


def validate_codex_subscription_version(version_output: str) -> None:
    """Require the exact release Codex CLI identity."""

    if version_output.strip() != f"codex-cli {CODEX_SUBSCRIPTION_CODEX_VERSION}":
        raise ValueError("Codex subscription CLI version is not the release pin")


def codex_subscription_cli_overrides(
    *,
    allow_internet: bool,
    tools_enabled: bool = True,
) -> tuple[str, ...]:
    """Return final, highest-precedence Codex 0.144.1 config overrides."""

    if not isinstance(allow_internet, bool):
        raise TypeError("Codex subscription network policy must be Core-owned")
    if not isinstance(tools_enabled, bool):
        raise TypeError("Codex subscription tool policy must be Core-owned")
    filesystem = ",".join(
        f"{json.dumps(path)}={json.dumps(access)}" for path, access in _FILESYSTEM_POLICY
    )
    shell_set = ",".join(
        f"{key}={json.dumps(value)}"
        for key, value in (
            ("HOME", MANAGED_HOME),
            ("PATH", MANAGED_PATH),
            ("TMPDIR", "/tmp"),
            *_EVOLUTION_SHELL_ENV,
        )
    )
    # Subscription provider transport still requires network access.  A
    # completion-only run independently removes every model-visible execution
    # and search surface while retaining that provider transport.
    web_search = "live" if allow_internet and tools_enabled else "disabled"
    overrides = [
        f"default_permissions={json.dumps(CODEX_SUBSCRIPTION_PERMISSION_PROFILE)}",
        (f"permissions.{CODEX_SUBSCRIPTION_PERMISSION_PROFILE}.filesystem={{{filesystem}}}"),
        (
            f"permissions.{CODEX_SUBSCRIPTION_PERMISSION_PROFILE}.network.enabled="
            f"{str(allow_internet).lower()}"
        ),
        'approval_policy="never"',
        "allow_login_shell=false",
        "check_for_update_on_startup=false",
        'cli_auth_credentials_store="file"',
        'forced_login_method="chatgpt"',
        f'shell_environment_policy={{inherit="none",set={{{shell_set}}}}}',
        f'projects.{json.dumps(MANAGED_WORKSPACE)}.trust_level="untrusted"',
        'model_provider="openai"',
        f'web_search="{web_search}"',
        "include_apps_instructions=false",
        "include_collaboration_mode_instructions=false",
        "mcp_servers={}",
        "plugins={}",
        "marketplaces={}",
        "profiles={}",
        f"features.shell_tool={str(tools_enabled).lower()}",
        f"features.unified_exec={str(tools_enabled).lower()}",
    ]
    overrides.extend(f"features.{feature}=false" for feature in _DISABLED_EXECUTION_FEATURES)
    return tuple(overrides)


def codex_subscription_cli_flags(
    *,
    allow_internet: bool,
    tools_enabled: bool = True,
) -> tuple[str, ...]:
    """Render the closed CLI and config profile as shell-safe arguments."""

    return (
        "--strict-config",
        "--ignore-user-config",
        "--ignore-rules",
        *(
            f"-c {shlex.quote(override)}"
            for override in codex_subscription_cli_overrides(
                allow_internet=allow_internet,
                tools_enabled=tools_enabled,
            )
        ),
    )


_CANARY_SCRIPT_AUTHORITY_SOURCE: Final[str] = r"""
import hashlib
import os
import stat
import sys

operation, path, *rest = sys.argv[1:]
max_bytes = 65536
flags = getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)

def fail():
    raise SystemExit("Codex subscription canary script authority is invalid")

def identity(metadata, digest):
    return "{}:{}:{}:{}".format(
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        digest,
    )

if operation == "publish":
    if len(rest) != 1:
        fail()
    payload = rest[0].encode("utf-8")
    if not payload or len(payload) > max_bytes or b"\x00" in payload:
        fail()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | flags, 0o500)
        try:
            os.fchmod(fd, 0o500)
            written = 0
            while written < len(payload):
                count = os.write(fd, payload[written:])
                if count <= 0:
                    fail()
                written += count
            os.fsync(fd)
            metadata = os.fstat(fd)
        finally:
            os.close(fd)
    except OSError:
        fail()
    digest = hashlib.sha256(payload).hexdigest()
elif operation == "verify":
    if rest:
        fail()
    try:
        fd = os.open(path, os.O_RDONLY | flags)
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.geteuid()
                or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != 0o500
                or before.st_size <= 0
                or before.st_size > max_bytes
            ):
                fail()
            payload = b""
            while len(payload) <= before.st_size:
                chunk = os.read(fd, min(65536, before.st_size - len(payload) + 1))
                if not chunk:
                    break
                payload += chunk
            after = os.fstat(fd)
        finally:
            os.close(fd)
        path_metadata = os.lstat(path)
    except OSError:
        fail()
    if (
        len(payload) != before.st_size
        or (before.st_dev, before.st_ino, before.st_size)
        != (after.st_dev, after.st_ino, after.st_size)
        or (before.st_dev, before.st_ino)
        != (path_metadata.st_dev, path_metadata.st_ino)
        or not stat.S_ISREG(path_metadata.st_mode)
        or stat.S_IMODE(path_metadata.st_mode) != 0o500
    ):
        fail()
    metadata = after
    digest = hashlib.sha256(payload).hexdigest()
else:
    fail()

print(identity(metadata, digest))
"""


_CANARY_INVENTORY_SOURCE: Final[str] = r"""
import os
import stat
import sys

prefix, workspace, home, tmpdir, credential_root, *expected = sys.argv[1:]
expected_paths = set(expected)
observed = set()
for root in (workspace, home, tmpdir, credential_root):
    try:
        entries = os.scandir(root)
        with entries:
            for entry in entries:
                if not entry.name.startswith(prefix):
                    continue
                path = os.path.join(root, entry.name)
                metadata = os.lstat(path)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise SystemExit(
                        "Codex subscription canary inventory is invalid"
                    )
                observed.add(path)
    except OSError:
        raise SystemExit("Codex subscription canary inventory is invalid")
if observed != expected_paths:
    raise SystemExit("Codex subscription canary inventory is invalid")
"""


def codex_subscription_exec_canary_command(
    *,
    model: str = MANAGED_CODEX_DEFAULT_MODEL,
    allow_internet: bool = True,
    nonce: str | None = None,
) -> str:
    """Build the trusted-parent real ``codex exec`` readiness probe."""

    checked_model = _require_cli_value(model, owner="model")
    checked_nonce = _new_or_checked_nonce(nonce)
    retry_nonce = hashlib.sha256(f"{checked_nonce}:retry".encode()).hexdigest()[
        : _CANARY_NONCE_BYTES * 2
    ]
    marker = CODEX_SUBSCRIPTION_CANARY_OK
    auth_file = f"{MANAGED_CODEX_HOME}/auth.json"
    profile_flags = " ".join(codex_subscription_cli_flags(allow_internet=allow_internet))
    attempts: list[dict[str, str]] = []
    attempt_nonces = (checked_nonce, retry_nonce)
    if len(attempt_nonces) != CODEX_SUBSCRIPTION_CANARY_ATTEMPTS:
        raise RuntimeError("Codex subscription canary attempt policy is invalid")
    validator_return_codes = [
        return_code for return_code, _ in _CANARY_VALIDATOR_FAILURE_CODES
    ]
    validator_failure_codes = [
        failure_code for _, failure_code in _CANARY_VALIDATOR_FAILURE_CODES
    ]
    if (
        len(set(validator_return_codes)) != len(validator_return_codes)
        or len(set(validator_failure_codes)) != len(validator_failure_codes)
        or any(
            return_code < 1
            or return_code > 125
            or return_code == _CANARY_CLEAN_REFUSAL_RC
            for return_code in validator_return_codes
        )
        or any(failure_code not in _CANARY_FAILURE_CODES for failure_code in validator_failure_codes)
    ):
        raise RuntimeError("Codex subscription canary validator policy is invalid")
    for attempt_number, attempt_nonce in enumerate(attempt_nonces, start=1):
        script_id = hashlib.sha256(f"{attempt_nonce}:script".encode()).hexdigest()[:24]
        prefix = f".openevo-{script_id}"
        expected_output = _canary_expected_output(attempt_nonce, marker)
        attempt = {
            "nonce": attempt_nonce,
            "script": f"{MANAGED_WORKSPACE}/{prefix}-probe.sh",
            "event": f"{MANAGED_CODEX_HOME}/{prefix}-events.jsonl",
            "stderr": f"{MANAGED_CODEX_HOME}/{prefix}-stderr",
            "workspace": f"{MANAGED_WORKSPACE}/{prefix}-workspace",
            "home_read": f"{MANAGED_HOME}/{prefix}-read",
            "home_write": f"{MANAGED_HOME}/{prefix}-write",
            "tmp": f"/tmp/{prefix}-write",
            "expected_output": expected_output,
            "number": str(attempt_number),
        }
        attempt["script_content"] = _canary_probe_script(
            auth_file=auth_file,
            workspace_canary=attempt["workspace"],
            home_read_canary=attempt["home_read"],
            home_write_canary=attempt["home_write"],
            tmp_canary=attempt["tmp"],
            nonce=attempt_nonce,
            expected_output=expected_output,
        )
        attempts.append(attempt)
    cleanup_paths = " ".join(
        shlex.quote(path)
        for attempt in attempts
        for path in (
            attempt["script"],
            attempt["event"],
            attempt["stderr"],
            attempt["workspace"],
            attempt["home_read"],
            attempt["home_write"],
            attempt["tmp"],
        )
    )
    lines = [
        "set -eu",
        "umask 077",
        "failure_code=LOCAL_PRECONDITION_FAILED",
        (
            "cleanup() { cleanup_status=$?; trap - EXIT HUP INT TERM; "
            f"if ! rm -f -- {cleanup_paths} 2>/dev/null; then "
            "failure_code=CLEANUP_FAILED; cleanup_status=1; fi; "
            'if test "$cleanup_status" -ne 0; then '
            f"printf '%s%s\\n' {shlex.quote(CODEX_SUBSCRIPTION_CANARY_FAILURE_PREFIX)} "
            '"$failure_code" >&2; fi; exit "$cleanup_status"; }'
        ),
        "on_signal() { failure_code=INTERRUPTED; trap - HUP INT TERM; exit 1; }",
        "trap cleanup EXIT",
        "trap on_signal HUP INT TERM",
        (
            f'test "$({shlex.quote(MANAGED_CODEX_BINARY)} --version)" = '
            f"{shlex.quote(f'codex-cli {CODEX_SUBSCRIPTION_CODEX_VERSION}')}"
        ),
        f"test -x {shlex.quote(MANAGED_CODEX_BINARY)}",
        f"test -d {shlex.quote(MANAGED_CODEX_PACKAGE_ROOT)}",
        f"test -r {shlex.quote(auth_file)}",
        f"/bin/cat {shlex.quote(auth_file)} >/dev/null",
        f"test -d {shlex.quote(CODEX_SUBSCRIPTION_CANARY_CWD)}",
        f"test ! -L {shlex.quote(CODEX_SUBSCRIPTION_CANARY_CWD)}",
        (
            f'test "$(stat -c %u {shlex.quote(CODEX_SUBSCRIPTION_CANARY_CWD)})" = '
            '"$(id -u)"'
        ),
        f'test "$(stat -c %a {shlex.quote(CODEX_SUBSCRIPTION_CANARY_CWD)})" = 700',
        (
            'test -z "$(find '
            f"{shlex.quote(CODEX_SUBSCRIPTION_CANARY_CWD)} "
            '-mindepth 1 -maxdepth 1 -print -quit)"'
        ),
        *(
            f"test ! -e {shlex.quote(directory + '/' + filename)}"
            for directory in (
                CODEX_SUBSCRIPTION_CANARY_CWD,
                MANAGED_HOME,
                "/openevo/session",
                "/openevo",
                "",
                MANAGED_CODEX_HOME,
            )
            for filename in ("AGENTS.override.md", "AGENTS.md")
        ),
        f"test ! -e {shlex.quote(MANAGED_HOME + '/.agents/skills')}",
        f"rm -f -- {cleanup_paths}",
        "canary_passed=0",
    ]
    for attempt in attempts:
        invocation = f"/bin/sh {shlex.quote(attempt['script'])} {shlex.quote(attempt['nonce'])}"
        prompt = (
            "You must invoke the shell tool exactly once with the exact command "
            "below. Permission denial inside the script is expected and is checked "
            "by the script itself. Do not inspect the script, do not use any other "
            "tool, and report only the command exit status.\n\n"
            f"{invocation}"
        )
        validator = _canary_event_validator_command(
            event_file=attempt["event"],
            stderr_file=attempt["stderr"],
            nonce=attempt["nonce"],
            marker=marker,
            script_path=attempt["script"],
            expected_output=attempt["expected_output"],
        )
        validator_failure_case = [
            '    case "$validator_rc" in',
            *(
                f"      {return_code}) failure_code={failure_code} ;;"
                for return_code, failure_code in _CANARY_VALIDATOR_FAILURE_CODES
            ),
            "      *) failure_code=VALIDATOR_INTERNAL_ERROR ;;",
            "    esac",
            "    exit 1",
        ]
        prefix = attempt["script"].rsplit("/", 1)[1].removesuffix("-probe.sh")
        refusal_inventory = _canary_inventory_command(
            prefix=prefix,
            expected_paths=(
                attempt["script"],
                attempt["event"],
                attempt["stderr"],
                attempt["home_read"],
            ),
        )
        success_inventory = _canary_inventory_command(
            prefix=prefix,
            expected_paths=(
                attempt["script"],
                attempt["event"],
                attempt["stderr"],
                attempt["workspace"],
                attempt["home_read"],
                attempt["tmp"],
            ),
        )
        lines.extend(
            [
                'if test "$canary_passed" -eq 0; then',
                "  failure_code=SCRIPT_AUTHORITY_INVALID",
                (
                    "  script_evidence=$("
                    f"python3 -c {shlex.quote(_CANARY_SCRIPT_AUTHORITY_SOURCE)} "
                    f"publish {shlex.quote(attempt['script'])} "
                    f"{shlex.quote(attempt['script_content'])})"
                ),
                (
                    f"  printf '%s' {shlex.quote(attempt['nonce'])} > "
                    f"{shlex.quote(attempt['home_read'])}"
                ),
                "  set +e",
                "  failure_code=VALIDATOR_INTERNAL_ERROR",
                (
                    f"  {shlex.quote(MANAGED_CODEX_BINARY)} exec "
                    "--skip-git-repo-check --json --ephemeral "
                    f"--model {shlex.quote(checked_model)} {profile_flags} "
                    f"{shlex.quote(prompt)} < /dev/null "
                    f"> {shlex.quote(attempt['event'])} "
                    f"2> {shlex.quote(attempt['stderr'])}"
                ),
                "  codex_rc=$?",
                "  set -e",
                "  failure_code=SCRIPT_AUTHORITY_INVALID",
                (
                    '  test "$script_evidence" = "$('
                    f"python3 -c {shlex.quote(_CANARY_SCRIPT_AUTHORITY_SOURCE)} "
                    f'verify {shlex.quote(attempt["script"])})"'
                ),
                "  set +e",
                "  failure_code=VALIDATOR_INTERNAL_ERROR",
                f"  {validator} >/dev/null 2>/dev/null",
                "  validator_rc=$?",
                "  set -e",
                '  if test "$validator_rc" -eq 0; then',
                '    if test "$codex_rc" -ne 0; then',
                "      failure_code=NONZERO_CLI_WITH_EXACT_EVIDENCE",
                "      exit 1",
                "    fi",
                "    failure_code=PROBE_OUTPUT_INVALID",
                (
                    f'    test "$(/bin/cat {shlex.quote(attempt["workspace"])})" = '
                    f"{shlex.quote(attempt['nonce'])}"
                ),
                (
                    f'    test "$(/bin/cat {shlex.quote(attempt["tmp"])})" = '
                    f"{shlex.quote(attempt['nonce'])}"
                ),
                f"    test ! -e {shlex.quote(attempt['home_write'])}",
                f"    {success_inventory}",
                "    canary_passed=1",
                (
                    '  elif test "$validator_rc" -ne '
                    f"{_CANARY_CLEAN_REFUSAL_RC}; then"
                ),
                *validator_failure_case,
                "  else",
                "    failure_code=REFUSAL_INVENTORY_INVALID",
                f"    {refusal_inventory}",
                *(
                    [
                        "    failure_code=CLEAN_REFUSAL_EXHAUSTED",
                        "    exit 1",
                    ]
                    if int(attempt["number"]) == CODEX_SUBSCRIPTION_CANARY_ATTEMPTS
                    else [
                        "    failure_code=RETRY_DELAY_INTERRUPTED",
                        f"    sleep {CODEX_SUBSCRIPTION_CANARY_RETRY_DELAY_SECONDS}",
                    ]
                ),
                "  fi",
                "fi",
            ]
        )
    lines.extend(
        [
            "failure_code=VALIDATOR_INTERNAL_ERROR",
            'test "$canary_passed" -eq 1',
            f"printf '%s\\n' {shlex.quote(marker)}",
        ]
    )
    return "\n".join(lines)


def _canary_inventory_command(
    *,
    prefix: str,
    expected_paths: Sequence[str],
) -> str:
    args = " ".join(
        shlex.quote(value)
        for value in (
            prefix,
            MANAGED_WORKSPACE,
            MANAGED_HOME,
            "/tmp",
            MANAGED_CODEX_HOME,
            *expected_paths,
        )
    )
    return f"python3 -c {shlex.quote(_CANARY_INVENTORY_SOURCE)} {args}"


def _canary_expected_output(nonce: str, marker: str) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "nonce": nonce,
            "marker": marker,
            "result": _CANARY_RESULT,
            "leak": False,
            "forbidden_file": False,
        },
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _canary_probe_script(
    *,
    auth_file: str,
    workspace_canary: str,
    home_read_canary: str,
    home_write_canary: str,
    tmp_canary: str,
    nonce: str,
    expected_output: str,
) -> str:
    return "\n".join(
        [
            "set -eu",
            'test "$#" -eq 1',
            f'test "$1" = {shlex.quote(nonce)}',
            f"auth_file={shlex.quote(auth_file)}",
            f"workspace_canary={shlex.quote(workspace_canary)}",
            f"home_read_canary={shlex.quote(home_read_canary)}",
            f"home_write_canary={shlex.quote(home_write_canary)}",
            f"tmp_canary={shlex.quote(tmp_canary)}",
            'test ! -r "$auth_file"',
            '! /bin/cat "$auth_file" >/dev/null 2>&1',
            "for root_link in /proc/self/root /proc/[0-9]*/root; do",
            '  test -e "$root_link" || continue',
            '  candidate="$root_link$auth_file"',
            '  test ! -r "$candidate"',
            '  ! /bin/cat "$candidate" >/dev/null 2>&1',
            "done",
            "if command -v sudo >/dev/null 2>&1; then",
            '  ! sudo -n /bin/cat "$auth_file" >/dev/null 2>&1',
            "  for root_link in /proc/self/root /proc/[0-9]*/root; do",
            '    test -e "$root_link" || continue',
            '    ! sudo -n /bin/cat "$root_link$auth_file" >/dev/null 2>&1',
            "  done",
            "fi",
            f"printf '%s' {shlex.quote(nonce)} > \"$workspace_canary\"",
            (f'test "$(/bin/cat "$home_read_canary")" = {shlex.quote(nonce)}'),
            ("if (printf '%s' blocked > \"$home_write_canary\") 2>/dev/null; then exit 20; fi"),
            'test ! -e "$home_write_canary"',
            f"printf '%s' {shlex.quote(nonce)} > \"$tmp_canary\"",
            f"printf '%s\\n' {shlex.quote(expected_output)}",
        ]
    )


_CANARY_EVENT_VALIDATOR_SOURCE: Final[str] = r"""
import json
import os
import shlex
import stat
import sys

(
    event_path,
    stderr_path,
    nonce,
    marker,
    script_path,
    expected_output,
    *validator_return_codes,
) = sys.argv[1:]
max_bytes = 262144
stdin_notice = "Reading additional input from stdin..."
CLEAN_REFUSAL = 42

try:
    (
        CAPTURE_INTEGRITY_INVALID,
        EVENT_PROTOCOL_INVALID,
        TERMINAL_FAILURE_EVENT,
        COMMAND_IDENTITY_INVALID,
        COMMAND_LIFECYCLE_INVALID,
        PROBE_RESULT_INVALID,
        UNEXPECTED_TOOL_EVENT,
        VALIDATOR_CONTRACT_INVALID,
    ) = tuple(int(value) for value in validator_return_codes)
except (TypeError, ValueError):
    raise SystemExit(1)

def fail(return_code):
    raise SystemExit(return_code)

def identity(metadata):
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )

def read_regular(path, *, allow_empty):
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_size > max_bytes
                or (not allow_empty and before.st_size == 0)
            ):
                fail(CAPTURE_INTEGRITY_INVALID)
            payload = b""
            while len(payload) <= before.st_size:
                chunk = os.read(fd, min(65536, before.st_size - len(payload) + 1))
                if not chunk:
                    break
                payload += chunk
            after = os.fstat(fd)
        finally:
            os.close(fd)
        path_metadata = os.lstat(path)
    except (OSError, ValueError):
        fail(CAPTURE_INTEGRITY_INVALID)
    if (
        len(payload) != before.st_size
        or identity(before) != identity(after)
        or identity(before) != identity(path_metadata)
        or not stat.S_ISREG(path_metadata.st_mode)
        or path_metadata.st_nlink != 1
    ):
        fail(CAPTURE_INTEGRITY_INVALID)
    return payload

# Stderr is a bounded private diagnostic channel, not readiness evidence.  Its
# bytes are deliberately discarded and never enter a durable error or receipt.
read_regular(stderr_path, allow_empty=True)
payload = read_regular(event_path, allow_empty=False)
if not payload.endswith(b"\n"):
    fail(CAPTURE_INTEGRITY_INVALID)
try:
    lines = payload.decode("utf-8").splitlines()
except UnicodeError:
    fail(CAPTURE_INTEGRITY_INVALID)
if lines and lines[0] == stdin_notice:
    lines = lines[1:]
if not lines or any(not line for line in lines):
    fail(EVENT_PROTOCOL_INVALID)

try:
    evidence = json.loads(expected_output)
except (json.JSONDecodeError, RecursionError):
    fail(VALIDATOR_CONTRACT_INVALID)
if evidence != {
    "schema_version": 1,
    "nonce": nonce,
    "marker": marker,
    "result": "isolated",
    "leak": False,
    "forbidden_file": False,
}:
    fail(VALIDATOR_CONTRACT_INVALID)

counts = {
    "thread.started": 0,
    "turn.started": 0,
    "turn.completed": 0,
}
turn_active = False
last_event_type = None
command_started = []
command_completed = []
expected_invocation = ["/bin/sh", script_path, nonce]

def command_matches(command):
    try:
        parsed = shlex.split(command)
    except ValueError:
        return False
    if parsed == expected_invocation:
        return True
    if (
        len(parsed) == 3
        and parsed[0] in {"/bin/bash", "/bin/sh"}
        and parsed[1] in {"-c", "-lc"}
    ):
        try:
            return shlex.split(parsed[2]) == expected_invocation
        except ValueError:
            return False
    return False

for raw_line in lines:
    if len(raw_line.encode("utf-8")) > max_bytes:
        fail(EVENT_PROTOCOL_INVALID)
    try:
        event = json.loads(raw_line)
    except (json.JSONDecodeError, RecursionError):
        fail(EVENT_PROTOCOL_INVALID)
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        fail(EVENT_PROTOCOL_INVALID)
    event_type = event["type"]
    if event_type == "thread.started":
        if (
            any(counts.values())
            or turn_active
            or not isinstance(event.get("thread_id"), str)
            or not event["thread_id"]
        ):
            fail(EVENT_PROTOCOL_INVALID)
        counts[event_type] += 1
    elif event_type == "turn.started":
        if counts["thread.started"] != 1 or any(
            counts[name] for name in ("turn.started", "turn.completed")
        ) or turn_active:
            fail(EVENT_PROTOCOL_INVALID)
        counts[event_type] += 1
        turn_active = True
    elif event_type in {"item.started", "item.updated", "item.completed"}:
        item = event.get("item")
        if (
            not turn_active
            or not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or not item["id"]
            or not isinstance(item.get("type"), str)
            or not item["type"]
        ):
            fail(EVENT_PROTOCOL_INVALID)
        item_type = item["type"]
        if item_type == "command_execution":
            command = item.get("command")
            if (
                not isinstance(command, str)
                or not command_matches(command)
                or command.count(script_path) != 1
                or command.count(nonce) != 1
            ):
                fail(COMMAND_IDENTITY_INVALID)
            identity_pair = (item["id"], command)
            if event_type == "item.started":
                if (
                    command_started
                    or command_completed
                    or item.get("status") != "in_progress"
                    or item.get("aggregated_output") != ""
                    or item.get("exit_code") is not None
                ):
                    fail(COMMAND_LIFECYCLE_INVALID)
                command_started.append(identity_pair)
            elif event_type == "item.updated":
                update_output = item.get("aggregated_output")
                if (
                    command_started != [identity_pair]
                    or command_completed
                    or item.get("status") != "in_progress"
                    or item.get("exit_code") is not None
                    or not isinstance(update_output, str)
                    or not (expected_output + "\n").startswith(update_output)
                ):
                    fail(COMMAND_LIFECYCLE_INVALID)
            else:
                if command_started != [identity_pair] or command_completed:
                    fail(COMMAND_LIFECYCLE_INVALID)
                if (
                    item.get("status") != "completed"
                    or item.get("exit_code") != 0
                    or item.get("aggregated_output") != expected_output + "\n"
                ):
                    fail(PROBE_RESULT_INVALID)
                command_completed.append(identity_pair)
        elif item_type not in {"reasoning", "agent_message"}:
            fail(UNEXPECTED_TOOL_EVENT)
    elif event_type == "turn.completed":
        usage = event.get("usage")
        usage_fields = {
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
        }
        if (
            not turn_active
            or counts[event_type] != 0
            or not isinstance(usage, dict)
            or set(usage) != usage_fields
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in usage.values()
            )
        ):
            fail(EVENT_PROTOCOL_INVALID)
        counts[event_type] += 1
        turn_active = False
    elif event_type in {"turn.failed", "error"}:
        fail(TERMINAL_FAILURE_EVENT)
    else:
        fail(EVENT_PROTOCOL_INVALID)
    last_event_type = event_type

if (
    counts != {"thread.started": 1, "turn.started": 1, "turn.completed": 1}
    or turn_active
    or last_event_type != "turn.completed"
):
    fail(EVENT_PROTOCOL_INVALID)
if not command_started and not command_completed:
    fail(CLEAN_REFUSAL)
if len(command_started) != 1 or command_started != command_completed:
    fail(COMMAND_LIFECYCLE_INVALID)
"""


def _canary_event_validator_command(
    *,
    event_file: str,
    stderr_file: str,
    nonce: str,
    marker: str,
    script_path: str,
    expected_output: str,
) -> str:
    args = " ".join(
        shlex.quote(value)
        for value in (
            event_file,
            stderr_file,
            nonce,
            marker,
            script_path,
        expected_output,
            *(str(return_code) for return_code, _ in _CANARY_VALIDATOR_FAILURE_CODES),
        )
    )
    return f"python3 -c {shlex.quote(_CANARY_EVENT_VALIDATOR_SOURCE)} {args}"


def _new_or_checked_nonce(nonce: str | None) -> str:
    candidate = secrets.token_hex(_CANARY_NONCE_BYTES) if nonce is None else nonce
    if (
        not isinstance(candidate, str)
        or len(candidate) != _CANARY_NONCE_BYTES * 2
        or any(character not in "0123456789abcdef" for character in candidate)
    ):
        raise ValueError("Codex subscription canary nonce is invalid")
    return candidate


def _require_cli_value(value: str, *, owner: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in value)
    ):
        raise ValueError(f"Codex subscription {owner} is invalid")
    return value


__all__ = [
    "CODEX_SUBSCRIPTION_CANARY_ATTEMPTS",
    "CODEX_SUBSCRIPTION_CANARY_CWD",
    "CODEX_SUBSCRIPTION_CANARY_FAILURE_PREFIX",
    "CODEX_SUBSCRIPTION_CANARY_OK",
    "CODEX_SUBSCRIPTION_CANARY_RETRY_DELAY_SECONDS",
    "CODEX_SUBSCRIPTION_CANARY_VALIDATOR_SCHEMA",
    "CODEX_SUBSCRIPTION_CODEX_VERSION",
    "CODEX_SUBSCRIPTION_CONTRACT_KEY",
    "CODEX_SUBSCRIPTION_PERMISSION_PROFILE",
    "CODEX_SUBSCRIPTION_POLICY_ID",
    "CODEX_SUBSCRIPTION_POLICY_SHA256",
    "CODEX_SUBSCRIPTION_READINESS_KEY",
    "CODEX_SUBSCRIPTION_REFRESH_PERSISTENCE",
    "CODEX_SUBSCRIPTION_SANDBOX_BACKEND",
    "CODEX_SUBSCRIPTION_TOOL_POLICY_DISABLED",
    "CODEX_SUBSCRIPTION_TOOL_POLICY_KEY",
    "codex_subscription_canary_failure_code",
    "codex_subscription_cli_flags",
    "codex_subscription_cli_overrides",
    "codex_subscription_contract",
    "codex_subscription_exec_canary_command",
    "codex_subscription_readiness_receipt",
    "validate_codex_subscription_surface",
    "validate_codex_subscription_version",
]
