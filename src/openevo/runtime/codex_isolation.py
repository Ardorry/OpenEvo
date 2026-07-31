"""Closed Codex subscription credential-isolation policy."""

from __future__ import annotations

import hashlib
import json
import re
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

CODEX_SUBSCRIPTION_POLICY_ID: Final[str] = "openevo.codex-subscription-credential-isolation.v2"
CODEX_SUBSCRIPTION_PERMISSION_PROFILE: Final[str] = "openevo_codex_subscription_v1"
CODEX_SUBSCRIPTION_CODEX_VERSION: Final[str] = MANAGED_CODEX_VERSION
CODEX_SUBSCRIPTION_SANDBOX_BACKEND: Final[str] = "linux-bubblewrap"
CODEX_SUBSCRIPTION_CONTRACT_KEY: Final[str] = "credential_isolation"
CODEX_SUBSCRIPTION_READINESS_KEY: Final[str] = "credential_isolation_receipt"
CODEX_SUBSCRIPTION_CANARY_OK: Final[str] = (
    "openevo-codex-subscription-deterministic-sandbox-ready-v2"
)
CODEX_SUBSCRIPTION_REFRESH_PERSISTENCE: Final[str] = "unsupported_read_only_auth_overlay"
CODEX_SUBSCRIPTION_CANARY_CWD: Final[str] = MANAGED_CODEX_READINESS_WORKSPACE
_CANARY_RESULT: Final[str] = "isolated"
_CANARY_NONCE_BYTES: Final[int] = 16
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
    # Codex 0.144.1 resolves Core-owned, non-secret configuration below
    # CODEX_HOME while constructing its nested sandbox.  Keep that directory
    # readable, but deny the exact credential leaf to every tool process.
    (MANAGED_CODEX_HOME, "read"),
    (f"{MANAGED_CODEX_HOME}/auth.json", "deny"),
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
    "readiness_command": "codex login status + codex sandbox",
    "readiness_context": {
        "working_directory": CODEX_SUBSCRIPTION_CANARY_CWD,
        "project_instructions": "absent",
        "evolution_skills": "not_installed",
    },
    "readiness_evidence": "deterministic_no_model_sandbox",
    "readiness_canaries": [
        "exact_codex_version",
        "parent_auth_read",
        "tool_direct_auth_read_denied",
        "tool_proc_self_root_auth_read_denied",
        "tool_proc_pid_root_auth_read_denied",
        "tool_proc_environment_secret_absent",
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

# Lifecycle-28 stored results were sealed with this exact v1 authority.  It is
# accepted only for immutable database readback; new launches always require
# the current v2 contract and deterministic no-model proof.
_HISTORICAL_V1_CONTRACT: Final[dict[str, object]] = {
    "schema_version": 1,
    "policy_id": "openevo.codex-subscription-credential-isolation.v1",
    "policy_sha256": "59ea503b553aa414ddcc35ede66210ee901621eebcbd1cfbeb06023410e35d38",
    "permission_profile": CODEX_SUBSCRIPTION_PERMISSION_PROFILE,
    "codex_version": CODEX_SUBSCRIPTION_CODEX_VERSION,
    "default_model": MANAGED_CODEX_DEFAULT_MODEL,
    "sandbox_backend": CODEX_SUBSCRIPTION_SANDBOX_BACKEND,
    "refresh_persistence": CODEX_SUBSCRIPTION_REFRESH_PERSISTENCE,
}
_HISTORICAL_V1_RECEIPT: Final[dict[str, object]] = {
    **_HISTORICAL_V1_CONTRACT,
    "status": "passed",
    "canary": "openevo-codex-subscription-real-exec-ready-v1",
    "evidence": "completed_command_execution_event",
}


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
    """Return the receipt published only after the deterministic probe passes."""

    return {
        **codex_subscription_contract(),
        "status": "passed",
        "canary": CODEX_SUBSCRIPTION_CANARY_OK,
        "evidence": "deterministic_no_model_sandbox",
        "auth_status_checked": True,
        "model_started": False,
    }


def is_historical_codex_subscription_authority(
    contract: object,
    receipt: object,
) -> bool:
    """Recognize the one frozen v1 authority for immutable result readback."""

    return contract == _HISTORICAL_V1_CONTRACT and receipt == _HISTORICAL_V1_RECEIPT


class CodexSubscriptionIsolationError(RuntimeError):
    """Closed, non-secret failure emitted by the deterministic isolation probe."""

    _CODES: Final[frozenset[str]] = frozenset(
        {
            "parent_cli_version_mismatch",
            "parent_auth_unreadable",
            "parent_auth_status_unavailable",
            "readiness_workspace_invalid",
            "project_instructions_present",
            "runtime_skills_present",
            "sandbox_direct_credential_visible",
            "sandbox_proc_credential_visible",
            "sandbox_host_credential_visible",
            "sandbox_docker_socket_visible",
            "sandbox_environment_secret_visible",
            "sandbox_parent_environment_secret_visible",
            "sandbox_workspace_write_failed",
            "sandbox_home_read_failed",
            "sandbox_home_write_allowed",
            "sandbox_tmp_write_failed",
            "sandbox_namespace_unavailable",
            "sandbox_executable_unavailable",
            "sandbox_bwrap_unavailable",
            "sandbox_shell_unavailable",
            "sandbox_noop_executable_unavailable",
            "sandbox_helper_executable_unavailable",
            "sandbox_child_executable_unavailable",
            "sandbox_cli_contract_invalid",
            "sandbox_config_permission_denied",
            "sandbox_child_not_started",
            "sandbox_child_signalled",
            "sandbox_permission_denied",
            "sandbox_policy_rejected",
            "sandbox_read_only_filesystem",
            "sandbox_runtime_dependency_unavailable",
            "sandbox_security_policy_denied",
            "sandbox_timeout",
            "sandbox_user_namespace_denied",
            "sandbox_outer_command_failed",
            "sandbox_invocation_failed",
            "sandbox_output_invalid",
        }
    )

    _PROGRESS: Final[frozenset[str]] = frozenset(
        {
            "not_started",
            "outer_started",
            "parent_cli_ready",
            "parent_auth_readable",
            "parent_auth_ready",
            "workspace_ready",
            "instructions_clean",
            "uncredentialed_smoke",
            "credential_smoke",
            "sandbox_launched",
            "sandbox_returned",
            "classified",
            "entered",
            "direct_hidden",
            "host_hidden",
            "proc_hidden",
            "socket_hidden",
            "env_clean",
            "parent_env_clean",
            "workspace_write",
            "home_contract",
            "tmp_write",
            "completed",
            "invalid",
        }
    )
    _STDERR_CLASSES: Final[frozenset[str]] = frozenset(
        {
            "empty",
            "namespace_permission",
            "executable_missing",
            "cli_contract",
            "config_permission",
            "config_invalid",
            "working_directory",
            "policy_rejected",
            "operation_not_permitted",
            "permission_denied",
            "read_only_filesystem",
            "security_backend",
            "timeout",
            "outer_command_missing",
            "outer_missing_bash",
            "outer_missing_cat",
            "outer_missing_codex",
            "outer_missing_grep",
            "outer_missing_id",
            "outer_missing_rm",
            "outer_missing_sh",
            "outer_missing_stat",
            "outer_missing_tr",
            "unclassified",
        }
    )
    _HELPER_MISSING_EXECUTABLES: Final[frozenset[str]] = frozenset(
        {
            "none",
            "bwrap",
            "bash",
            "sh",
            "true",
            "env",
            "codex-linux-sandbox",
            "unclassified",
        }
    )

    def __init__(
        self,
        code: str,
        *,
        nested_return_code: int | None = None,
        probe_progress: str | None = None,
        stderr_class: str | None = None,
        helper_started: bool | None = None,
        helper_return_code: int | None = None,
        missing_executable_basename: str | None = None,
    ) -> None:
        if code not in self._CODES:
            code = "sandbox_invocation_failed"
        if nested_return_code is not None and not 0 <= nested_return_code <= 255:
            nested_return_code = None
        if probe_progress not in self._PROGRESS:
            probe_progress = None
        if stderr_class not in self._STDERR_CLASSES:
            stderr_class = None
        if type(helper_started) is not bool:
            helper_started = None
        if helper_return_code is not None and not 0 <= helper_return_code <= 255:
            helper_return_code = None
        if missing_executable_basename not in self._HELPER_MISSING_EXECUTABLES:
            missing_executable_basename = None
        super().__init__(f"CANDIDATE_SUBSCRIPTION_ISOLATION_NOT_READY:{code}")
        self.code = code
        self.nested_return_code = nested_return_code
        self.probe_progress = probe_progress
        self.stderr_class = stderr_class
        self.helper_started = helper_started
        self.helper_return_code = helper_return_code
        self.missing_executable_basename = missing_executable_basename


_ISOLATION_FAILURE_PREFIX: Final[str] = "OPENEVO_CODEX_ISOLATION_FAILURE:"
_ISOLATION_FAILURE_DIAGNOSTIC = re.compile(
    rf"^{re.escape(_ISOLATION_FAILURE_PREFIX)}"
    r"(?P<code>[a-z][a-z0-9_]{0,95})"
    r"\|nested_return_code=(?P<return_code>[0-9]{1,3})"
    r"\|progress=(?P<progress>[a-z][a-z0-9_]{0,31})"
    r"\|stderr_class=(?P<stderr_class>[a-z][a-z0-9_]{0,47})$"
)


def _codex_subscription_sandbox_command(
    *,
    allow_internet: bool,
    child_argv: tuple[str, ...],
) -> str:
    """Render the one production Codex sandbox invocation contract."""

    if not child_argv or any(
        not isinstance(value, str) or not value for value in child_argv
    ):
        raise ValueError("Codex subscription sandbox child argv is invalid")
    config_flags = " ".join(
        f"-c {shlex.quote(value)}"
        for value in codex_subscription_cli_overrides(allow_internet=allow_internet)
    )
    child = " ".join(shlex.quote(value) for value in child_argv)
    return (
        f"{shlex.quote(MANAGED_CODEX_BINARY)} {config_flags} sandbox "
        "--include-managed-config "
        f"--permission-profile {shlex.quote(CODEX_SUBSCRIPTION_PERMISSION_PROFILE)} "
        f"-C {shlex.quote(CODEX_SUBSCRIPTION_CANARY_CWD)} -- {child}"
    )


def codex_subscription_sandbox_smoke_command(*, allow_internet: bool) -> str:
    """Return the same production sandbox launcher with a no-op child."""

    return _codex_subscription_sandbox_command(
        allow_internet=allow_internet,
        child_argv=("/bin/true",),
    )


def validate_codex_subscription_sandbox_smoke_result(
    *,
    return_code: int,
    stderr: str | None,
    probe_progress: str,
    helper_started: bool | None = None,
    helper_return_code: int | None = None,
    missing_executable_basename: str | None = None,
) -> None:
    """Fail closed with bounded diagnostics when the no-op child cannot launch."""

    if return_code == 0:
        return
    safe_return_code = return_code if 0 <= return_code <= 255 else None
    stderr_class = _classify_outer_probe_stderr(stderr)
    code = "sandbox_child_not_started"
    if return_code >= 128:
        code = "sandbox_child_signalled"
    elif stderr_class == "namespace_permission":
        code = "sandbox_namespace_unavailable"
    elif stderr_class == "permission_denied":
        code = "sandbox_permission_denied"
    elif stderr_class == "operation_not_permitted":
        code = "sandbox_user_namespace_denied"
    elif stderr_class == "security_backend":
        code = "sandbox_security_policy_denied"
    elif missing_executable_basename == "bwrap":
        code = "sandbox_bwrap_unavailable"
    elif missing_executable_basename in {"bash", "sh"}:
        code = "sandbox_shell_unavailable"
    elif missing_executable_basename == "true":
        code = "sandbox_noop_executable_unavailable"
    elif missing_executable_basename == "codex-linux-sandbox":
        code = "sandbox_helper_executable_unavailable"
    elif missing_executable_basename in {"env", "unclassified"}:
        code = "sandbox_child_executable_unavailable"
    elif stderr_class == "executable_missing" or return_code == 127:
        code = "sandbox_executable_unavailable"
    raise CodexSubscriptionIsolationError(
        code,
        nested_return_code=safe_return_code,
        probe_progress=probe_progress,
        stderr_class=stderr_class,
        helper_started=helper_started,
        helper_return_code=helper_return_code,
        missing_executable_basename=missing_executable_basename,
    )


_SANDBOX_HELPER_DIAGNOSTIC = re.compile(
    r"\Astarted=(?P<started>[01])\n"
    r"return_code=(?P<return_code>[0-9]{1,3})\n"
    r"missing_executable=(?P<missing>none|bwrap|bash|sh|true|env|"
    r"codex-linux-sandbox|unclassified)\n?\Z"
)


def parse_codex_sandbox_helper_diagnostic(
    value: str | None,
) -> tuple[bool | None, int | None, str | None]:
    """Parse the optional closed diagnostic emitted by a managed helper wrapper."""

    match = _SANDBOX_HELPER_DIAGNOSTIC.fullmatch(value or "")
    if match is None:
        return None, None, None
    return_code = int(match.group("return_code"))
    if return_code > 255:
        return None, None, None
    return match.group("started") == "1", return_code, match.group("missing")


def codex_subscription_isolation_probe_command(
    *,
    allow_internet: bool,
    nonce: str | None = None,
) -> str:
    """Build the no-model proof used by readiness and ``CodexHarness.setup``.

    ``codex login status`` is the positive parent-auth control. ``codex
    sandbox`` invokes the same production permission profile used for shell
    tools without starting a model turn.  The child proves that the mounted
    credential is absent through direct and ``/proc/*/root`` aliases while the
    intended workspace and temporary-file capabilities remain usable.
    """

    checked_nonce = _new_or_checked_nonce(nonce)
    marker = CODEX_SUBSCRIPTION_CANARY_OK
    auth_file = f"{MANAGED_CODEX_HOME}/auth.json"
    home_auth_file = f"{MANAGED_HOME}/.codex/auth.json"
    workspace_canary = f"{MANAGED_WORKSPACE}/.openevo-isolation-{checked_nonce}"
    home_read_canary = f"{MANAGED_HOME}/.openevo-isolation-read-{checked_nonce}"
    home_write_canary = f"{MANAGED_HOME}/.openevo-isolation-write-{checked_nonce}"
    tmp_canary = f"/tmp/.openevo-isolation-{checked_nonce}"
    sandbox_stderr = f"/tmp/.openevo-isolation-{checked_nonce}.stderr"
    progress_canary = f"{MANAGED_WORKSPACE}/.openevo-isolation-progress-{checked_nonce}"
    sandbox_script = "\n".join(
        [
            "set -eu",
            f"auth_file={shlex.quote(auth_file)}",
            f"home_auth_file={shlex.quote(home_auth_file)}",
            f"workspace_canary={shlex.quote(workspace_canary)}",
            f"home_read_canary={shlex.quote(home_read_canary)}",
            f"home_write_canary={shlex.quote(home_write_canary)}",
            f"tmp_canary={shlex.quote(tmp_canary)}",
            f"progress_canary={shlex.quote(progress_canary)}",
            'printf entered > "$progress_canary" || exit 46',
            'test ! -r "$auth_file" || exit 41',
            '! /bin/cat "$auth_file" >/dev/null 2>&1 || exit 41',
            'printf direct_hidden > "$progress_canary" || exit 46',
            'test ! -r "$home_auth_file" || exit 43',
            'test ! -r /root/.codex/auth.json || exit 43',
            'printf host_hidden > "$progress_canary" || exit 46',
            "for root_link in /proc/self/root /proc/[0-9]*/root; do",
            '  test -e "$root_link" || continue',
            '  candidate="$root_link$auth_file"',
            '  test ! -r "$candidate" || exit 42',
            '  ! /bin/cat "$candidate" >/dev/null 2>&1 || exit 42',
            "done",
            'printf proc_hidden > "$progress_canary" || exit 46',
            "test ! -e /var/run/docker.sock || exit 44",
            'printf socket_hidden > "$progress_canary" || exit 46',
            (
                "env | grep -E "
                + shlex.quote(
                    "^(OPENEVO_CORE_CONTROL_BEARER|JUDGE_API_KEY|OPENAI_API_KEY|"
                    "SSH_AUTH_SOCK|DOCKER_HOST)="
                )
                + " >/dev/null && exit 45 || :"
            ),
            'printf env_clean > "$progress_canary" || exit 46',
            "for process_env in /proc/[0-9]*/environ; do",
            '  test -r "$process_env" || continue',
            (
                "  /usr/bin/tr '\\000' '\\n' < \"$process_env\" 2>/dev/null "
                "| grep -E "
                + shlex.quote(
                    "^(OPENEVO_CORE_CONTROL_BEARER|JUDGE_API_KEY|OPENAI_API_KEY|"
                    "SSH_AUTH_SOCK|DOCKER_HOST)="
                )
                + " >/dev/null && exit 50 || :"
            ),
            "done",
            'printf parent_env_clean > "$progress_canary" || exit 46',
            'printf "%s" "' + checked_nonce + '" > "$workspace_canary" || exit 46',
            'printf workspace_write > "$progress_canary" || exit 46',
            'test "$(/bin/cat "$home_read_canary")" = "' + checked_nonce + '" || exit 47',
            '(printf blocked > "$home_write_canary") 2>/dev/null && exit 48 || :',
            'test ! -e "$home_write_canary" || exit 48',
            'printf home_contract > "$progress_canary" || exit 46',
            'printf "%s" "' + checked_nonce + '" > "$tmp_canary" || exit 49',
            'printf tmp_write > "$progress_canary" || exit 46',
            'printf completed > "$progress_canary" || exit 46',
            f"printf '%s\\n' {shlex.quote(marker)}",
        ]
    )
    sandbox_command = _codex_subscription_sandbox_command(
        allow_internet=allow_internet,
        child_argv=("/bin/sh", "-c", sandbox_script),
    )
    cleanup_paths = " ".join(
        shlex.quote(path)
        for path in (
            workspace_canary,
            home_read_canary,
            home_write_canary,
            tmp_canary,
            sandbox_stderr,
            progress_canary,
        )
    )
    return "\n".join(
        [
            "set -u",
            "umask 077",
            "diagnostic_emitted=0",
            "outer_progress=outer_started",
            f"cleanup() {{ /bin/rm -f -- {cleanup_paths}; }}",
            (
                "finish() { rc=$?; trap - EXIT; "
                "if test \"$rc\" -ne 0 && test \"$diagnostic_emitted\" -eq 0; then "
                "stderr_class=unclassified; "
                "if test \"$rc\" -eq 127; then stderr_class=outer_command_missing; fi; "
                "printf '%s%s|nested_return_code=%s|progress=%s|stderr_class=%s\\n' "
                f"{shlex.quote(_ISOLATION_FAILURE_PREFIX)} "
                "sandbox_outer_command_failed \"$rc\" \"$outer_progress\" "
                "\"$stderr_class\" >&2; fi; cleanup; exit \"$rc\"; }"
            ),
            "trap finish EXIT",
            "trap 'exit 129' HUP",
            "trap 'exit 130' INT",
            "trap 'exit 143' TERM",
            (
                "emit_diagnostic() { "
                "diagnostic_emitted=1; "
                "printf '%s%s|nested_return_code=%s|progress=%s|stderr_class=%s\\n' "
                f"{shlex.quote(_ISOLATION_FAILURE_PREFIX)} "
                '"$1" "$2" "$3" "$4" >&2; exit 70; }'
            ),
            (
                f'test "$({shlex.quote(MANAGED_CODEX_BINARY)} --version)" = '
                f"{shlex.quote(f'codex-cli {CODEX_SUBSCRIPTION_CODEX_VERSION}')} || "
                "emit_diagnostic parent_cli_version_mismatch 70 "
                '"$outer_progress" unclassified'
            ),
            "outer_progress=parent_cli_ready",
            f"test -r {shlex.quote(auth_file)} || "
            'emit_diagnostic parent_auth_unreadable 70 "$outer_progress" unclassified',
            f"/bin/cat {shlex.quote(auth_file)} >/dev/null || "
            'emit_diagnostic parent_auth_unreadable 70 "$outer_progress" unclassified',
            "outer_progress=parent_auth_readable",
            (
                f"{shlex.quote(MANAGED_CODEX_BINARY)} login status </dev/null "
                ">/dev/null 2>&1 || "
                "emit_diagnostic parent_auth_status_unavailable 70 "
                '"$outer_progress" unclassified'
            ),
            "outer_progress=parent_auth_ready",
            f"test -d {shlex.quote(CODEX_SUBSCRIPTION_CANARY_CWD)} || "
            'emit_diagnostic readiness_workspace_invalid 70 "$outer_progress" unclassified',
            f"test ! -L {shlex.quote(CODEX_SUBSCRIPTION_CANARY_CWD)} || "
            'emit_diagnostic readiness_workspace_invalid 70 "$outer_progress" unclassified',
            (
                f'test "$(/usr/bin/stat -c %u {shlex.quote(CODEX_SUBSCRIPTION_CANARY_CWD)})" = '
                '"$(/usr/bin/id -u)" || '
                "emit_diagnostic readiness_workspace_invalid 70 "
                '"$outer_progress" unclassified'
            ),
            "outer_progress=workspace_ready",
            f"test ! -e {shlex.quote(CODEX_SUBSCRIPTION_CANARY_CWD + '/AGENTS.md')} || "
            'emit_diagnostic project_instructions_present 70 "$outer_progress" unclassified',
            f"test ! -e {shlex.quote(MANAGED_HOME + '/.agents/skills')} || "
            'emit_diagnostic runtime_skills_present 70 "$outer_progress" unclassified',
            "outer_progress=instructions_clean",
            f"printf '%s' {shlex.quote(checked_nonce)} > {shlex.quote(home_read_canary)}",
            "set +e",
            "outer_progress=sandbox_launched",
            f"sandbox_output=$({sandbox_command} 2>{shlex.quote(sandbox_stderr)})",
            "sandbox_rc=$?",
            "outer_progress=sandbox_returned",
            "set -e",
            "progress=not_started",
            f"if test -r {shlex.quote(progress_canary)}; then",
            f"  progress=$(/bin/cat {shlex.quote(progress_canary)} 2>/dev/null || printf invalid)",
            "  case \"$progress\" in",
            (
                "    entered|direct_hidden|host_hidden|proc_hidden|socket_hidden|"
                "env_clean|parent_env_clean|workspace_write|home_contract|tmp_write|completed) ;;"
            ),
            "    *) progress=invalid ;;",
            "  esac",
            "fi",
            "stderr_class=unclassified",
            f"if test ! -s {shlex.quote(sandbox_stderr)}; then",
            "  stderr_class=empty",
            (
                f"elif grep -F {shlex.quote('No permissions to create a new namespace')} "
                f"{shlex.quote(sandbox_stderr)} >/dev/null 2>&1; then"
            ),
            "  stderr_class=namespace_permission",
            (
                f"elif grep -E {shlex.quote('required arguments were not provided|Usage: codex sandbox|unexpected argument|unrecognized option')} "
                f"{shlex.quote(sandbox_stderr)} >/dev/null 2>&1; then"
            ),
            "  stderr_class=cli_contract",
            (
                f"elif grep -F {shlex.quote('Failed to read config file')} "
                f"{shlex.quote(sandbox_stderr)} >/dev/null 2>&1 && "
                f"grep -F {shlex.quote('Permission denied')} "
                f"{shlex.quote(sandbox_stderr)} >/dev/null 2>&1; then"
            ),
            "  stderr_class=config_permission",
            (
                f"elif grep -Ei {shlex.quote('invalid (configuration|config)|config.*parse|TOML.*(parse|invalid)')} "
                f"{shlex.quote(sandbox_stderr)} >/dev/null 2>&1; then"
            ),
            "  stderr_class=config_invalid",
            (
                f"elif grep -Ei {shlex.quote('working directory|current directory|chdir')} "
                f"{shlex.quote(sandbox_stderr)} >/dev/null 2>&1; then"
            ),
            "  stderr_class=working_directory",
            (
                f"elif grep -Ei {shlex.quote('permission profile|default_permissions|managed requirements|sandbox policy')} "
                f"{shlex.quote(sandbox_stderr)} >/dev/null 2>&1; then"
            ),
            "  stderr_class=policy_rejected",
            (
                f"elif grep -Ei {shlex.quote('seccomp|landlock|bubblewrap|bwrap')} "
                f"{shlex.quote(sandbox_stderr)} >/dev/null 2>&1; then"
            ),
            "  stderr_class=security_backend",
            (
                f"elif grep -F {shlex.quote('Read-only file system')} "
                f"{shlex.quote(sandbox_stderr)} >/dev/null 2>&1; then"
            ),
            "  stderr_class=read_only_filesystem",
            (
                f"elif grep -F {shlex.quote('Operation not permitted')} "
                f"{shlex.quote(sandbox_stderr)} >/dev/null 2>&1; then"
            ),
            "  stderr_class=operation_not_permitted",
            (
                f"elif grep -F {shlex.quote('Permission denied')} "
                f"{shlex.quote(sandbox_stderr)} >/dev/null 2>&1; then"
            ),
            "  stderr_class=permission_denied",
            (
                f"elif grep -Ei {shlex.quote('timed out|timeout')} "
                f"{shlex.quote(sandbox_stderr)} >/dev/null 2>&1; then"
            ),
            "  stderr_class=timeout",
            (
                f"elif grep -F {shlex.quote('No such file or directory')} "
                f"{shlex.quote(sandbox_stderr)} >/dev/null 2>&1; then"
            ),
            "  stderr_class=executable_missing",
            "fi",
            "outer_progress=classified",
            "case \"$sandbox_rc\" in",
            "  0) ;;",
            "  41) emit_diagnostic sandbox_direct_credential_visible \"$sandbox_rc\" \"$progress\" \"$stderr_class\" ;;",
            "  42) emit_diagnostic sandbox_proc_credential_visible \"$sandbox_rc\" \"$progress\" \"$stderr_class\" ;;",
            "  43) emit_diagnostic sandbox_host_credential_visible \"$sandbox_rc\" \"$progress\" \"$stderr_class\" ;;",
            "  44) emit_diagnostic sandbox_docker_socket_visible \"$sandbox_rc\" \"$progress\" \"$stderr_class\" ;;",
            "  45) emit_diagnostic sandbox_environment_secret_visible \"$sandbox_rc\" \"$progress\" \"$stderr_class\" ;;",
            "  46) emit_diagnostic sandbox_workspace_write_failed \"$sandbox_rc\" \"$progress\" \"$stderr_class\" ;;",
            "  47) emit_diagnostic sandbox_home_read_failed \"$sandbox_rc\" \"$progress\" \"$stderr_class\" ;;",
            "  48) emit_diagnostic sandbox_home_write_allowed \"$sandbox_rc\" \"$progress\" \"$stderr_class\" ;;",
            "  49) emit_diagnostic sandbox_tmp_write_failed \"$sandbox_rc\" \"$progress\" \"$stderr_class\" ;;",
            "  50) emit_diagnostic sandbox_parent_environment_secret_visible \"$sandbox_rc\" \"$progress\" \"$stderr_class\" ;;",
            "  *)",
            "    case \"$stderr_class\" in",
            "      namespace_permission) failure=sandbox_namespace_unavailable ;;",
            "      executable_missing) failure=sandbox_executable_unavailable ;;",
            "      cli_contract) failure=sandbox_cli_contract_invalid ;;",
            "      config_permission) failure=sandbox_config_permission_denied ;;",
            "      policy_rejected|config_invalid) failure=sandbox_policy_rejected ;;",
            "      security_backend) failure=sandbox_security_policy_denied ;;",
            "      read_only_filesystem) failure=sandbox_read_only_filesystem ;;",
            "      operation_not_permitted) failure=sandbox_user_namespace_denied ;;",
            "      permission_denied) failure=sandbox_permission_denied ;;",
            "      timeout) failure=sandbox_timeout ;;",
            "      *) failure=sandbox_invocation_failed ;;",
            "    esac",
            "    if test \"$progress\" = not_started; then failure=sandbox_child_not_started; fi",
            "    if test \"$sandbox_rc\" -ge 128; then failure=sandbox_child_signalled; fi",
            "    emit_diagnostic \"$failure\" \"$sandbox_rc\" \"$progress\" \"$stderr_class\"",
            "    ;;",
            "esac",
            f"test \"$sandbox_output\" = {shlex.quote(marker)} || "
            'emit_diagnostic sandbox_output_invalid 70 "$progress" "$stderr_class"',
            f"printf '%s\\n' {shlex.quote(marker)}",
        ]
    )


def _failure_literal(code: str) -> str:
    return shlex.quote(_ISOLATION_FAILURE_PREFIX + code)


def validate_codex_subscription_isolation_result(
    *,
    return_code: int,
    stdout: str | None,
    stderr: str | None,
) -> None:
    """Validate a probe result without surfacing untrusted command output."""

    if return_code == 0 and (stdout or "").strip() == CODEX_SUBSCRIPTION_CANARY_OK:
        if (stderr or "").strip():
            raise CodexSubscriptionIsolationError("sandbox_output_invalid")
        return
    rendered = (stderr or "").strip().splitlines()
    code = "sandbox_invocation_failed"
    diagnostics = [
        diagnostic
        for line in rendered
        if (diagnostic := _ISOLATION_FAILURE_DIAGNOSTIC.fullmatch(line))
        is not None
    ]
    if len(diagnostics) == 1:
        diagnostic = diagnostics[0]
        candidate = diagnostic.group("code")
        nested_return_code = int(diagnostic.group("return_code"))
        progress = diagnostic.group("progress")
        stderr_class = diagnostic.group("stderr_class")
        if (
            candidate in CodexSubscriptionIsolationError._CODES
            and nested_return_code <= 255
            and progress in CodexSubscriptionIsolationError._PROGRESS
            and stderr_class in CodexSubscriptionIsolationError._STDERR_CLASSES
        ):
            raise CodexSubscriptionIsolationError(
                candidate,
                nested_return_code=nested_return_code,
                probe_progress=progress,
                stderr_class=stderr_class,
            )
    for line in rendered:
        if line.startswith(_ISOLATION_FAILURE_PREFIX):
            candidate = line[len(_ISOLATION_FAILURE_PREFIX) :]
            if candidate in CodexSubscriptionIsolationError._CODES:
                code = candidate
    safe_return_code = return_code if 0 <= return_code <= 255 else None
    fallback_stderr_class = _classify_outer_probe_stderr(stderr)
    if return_code == 127:
        code = "sandbox_outer_command_failed"
    raise CodexSubscriptionIsolationError(
        code,
        nested_return_code=safe_return_code,
        stderr_class=fallback_stderr_class,
    )


def _classify_outer_probe_stderr(stderr: str | None) -> str:
    """Classify transport stderr without retaining or surfacing its contents."""

    rendered = stderr or ""
    if not rendered.strip():
        return "empty"
    commands = {
        "bash": "outer_missing_bash",
        "cat": "outer_missing_cat",
        "codex": "outer_missing_codex",
        "grep": "outer_missing_grep",
        "id": "outer_missing_id",
        "rm": "outer_missing_rm",
        "sh": "outer_missing_sh",
        "stat": "outer_missing_stat",
        "tr": "outer_missing_tr",
    }
    for command, failure_class in commands.items():
        patterns = (
            f"{command}: command not found",
            f"{command}: not found",
            f'"{command}": executable file not found',
            f"/{command}: No such file or directory",
        )
        if any(pattern in rendered for pattern in patterns):
            return failure_class
    if "Permission denied" in rendered:
        return "permission_denied"
    if "Operation not permitted" in rendered:
        return "operation_not_permitted"
    if "No such file or directory" in rendered:
        return "executable_missing"
    return "unclassified"


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


def validate_codex_subscription_version(version_output: str) -> None:
    """Require the exact release Codex CLI identity."""

    if version_output.strip() != f"codex-cli {CODEX_SUBSCRIPTION_CODEX_VERSION}":
        raise ValueError("Codex subscription CLI version is not the release pin")


def codex_subscription_cli_overrides(
    *,
    allow_internet: bool,
) -> tuple[str, ...]:
    """Return final, highest-precedence Codex 0.144.1 config overrides."""

    if not isinstance(allow_internet, bool):
        raise ValueError("Codex subscription network policy must be Core-owned")
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
    web_search = "live" if allow_internet else "disabled"
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
        "features.shell_tool=true",
        "features.unified_exec=true",
    ]
    overrides.extend(f"features.{feature}=false" for feature in _DISABLED_EXECUTION_FEATURES)
    return tuple(overrides)


def codex_subscription_cli_flags(
    *,
    allow_internet: bool,
) -> tuple[str, ...]:
    """Render the closed CLI and config profile as shell-safe arguments."""

    return (
        "--strict-config",
        "--ignore-user-config",
        "--ignore-rules",
        *(
            f"-c {shlex.quote(override)}"
            for override in codex_subscription_cli_overrides(allow_internet=allow_internet)
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
    for attempt_number, attempt_nonce in enumerate((checked_nonce, retry_nonce), start=1):
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
        f"cleanup() {{ rm -f -- {cleanup_paths}; }}",
        "trap cleanup EXIT HUP INT TERM",
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
                (
                    '  test "$script_evidence" = "$('
                    f"python3 -c {shlex.quote(_CANARY_SCRIPT_AUTHORITY_SOURCE)} "
                    f'verify {shlex.quote(attempt["script"])})"'
                ),
                '  test "$codex_rc" -eq 0',
                "  set +e",
                f"  {validator}",
                "  validator_rc=$?",
                "  set -e",
                '  if test "$validator_rc" -eq 0; then',
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
                '  elif test "$validator_rc" -ne 42; then',
                "    exit 1",
                "  else",
                f"    {refusal_inventory}",
                f"    if test {attempt['number']} -eq 2; then exit 1; fi",
                "  fi",
                "fi",
            ]
        )
    lines.extend(
        [
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

event_path, stderr_path, nonce, marker, script_path, expected_output = sys.argv[1:]
max_bytes = 262144

def fail():
    raise SystemExit("Codex subscription real-exec evidence is invalid")

def clean_refusal():
    raise SystemExit(42)

def read_regular(path, *, allow_empty):
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        try:
            metadata = os.fstat(fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_size > max_bytes
                or (not allow_empty and metadata.st_size == 0)
            ):
                fail()
            payload = b""
            while len(payload) <= metadata.st_size:
                chunk = os.read(fd, min(65536, metadata.st_size - len(payload) + 1))
                if not chunk:
                    break
                payload += chunk
            if len(payload) != metadata.st_size:
                fail()
            return payload
        finally:
            os.close(fd)
    except (OSError, ValueError):
        fail()

stderr_payload = read_regular(stderr_path, allow_empty=True)
if stderr_payload not in {b"", b"Reading additional input from stdin...\n"}:
    fail()
payload = read_regular(event_path, allow_empty=False)
if not payload.endswith(b"\n"):
    fail()
try:
    text = payload.decode("utf-8")
except UnicodeError:
    fail()

counts = {
    "thread.started": 0,
    "turn.started": 0,
    "turn.completed": 0,
}
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

for raw_line in text.splitlines():
    if not raw_line or len(raw_line.encode("utf-8")) > max_bytes:
        fail()
    try:
        event = json.loads(raw_line)
    except (json.JSONDecodeError, RecursionError):
        fail()
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        fail()
    event_type = event["type"]
    if event_type in counts:
        counts[event_type] += 1
        continue
    if event_type in {"turn.failed", "error"}:
        fail()
    if event_type not in {"item.started", "item.updated", "item.completed"}:
        fail()
    item = event.get("item")
    if not isinstance(item, dict) or not isinstance(item.get("type"), str):
        fail()
    item_type = item["type"]
    if item_type == "command_execution":
        if event_type == "item.updated":
            fail()
        command = item.get("command")
        if (
            not isinstance(command, str)
            or not command_matches(command)
            or command.count(script_path) != 1
            or command.count(nonce) != 1
            or not isinstance(item.get("id"), str)
            or not item["id"]
        ):
            fail()
        if event_type == "item.started":
            if (
                item.get("status") != "in_progress"
                or item.get("aggregated_output") != ""
                or item.get("exit_code") is not None
            ):
                fail()
            command_started.append((item["id"], command))
        else:
            if (
                item.get("status") != "completed"
                or item.get("exit_code") != 0
                or item.get("aggregated_output") != expected_output + "\n"
            ):
                fail()
            command_completed.append((item["id"], command))
        continue
    if item_type not in {"reasoning", "agent_message"}:
        fail()
    if event_type == "item.updated":
        fail()
    item_text = item.get("text")
    if event_type == "item.completed" and not isinstance(item_text, str):
        fail()

if counts != {"thread.started": 1, "turn.started": 1, "turn.completed": 1}:
    fail()
if not command_started and not command_completed:
    clean_refusal()
if len(command_started) != 1 or command_started != command_completed:
    fail()
try:
    evidence = json.loads(expected_output)
except (json.JSONDecodeError, RecursionError):
    fail()
if evidence != {
    "schema_version": 1,
    "nonce": nonce,
    "marker": marker,
    "result": "isolated",
    "leak": False,
    "forbidden_file": False,
}:
    fail()
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
    "CODEX_SUBSCRIPTION_CANARY_OK",
    "CODEX_SUBSCRIPTION_CANARY_CWD",
    "CODEX_SUBSCRIPTION_CODEX_VERSION",
    "CODEX_SUBSCRIPTION_CONTRACT_KEY",
    "CODEX_SUBSCRIPTION_PERMISSION_PROFILE",
    "CODEX_SUBSCRIPTION_POLICY_ID",
    "CODEX_SUBSCRIPTION_POLICY_SHA256",
    "CODEX_SUBSCRIPTION_READINESS_KEY",
    "CODEX_SUBSCRIPTION_REFRESH_PERSISTENCE",
    "CODEX_SUBSCRIPTION_SANDBOX_BACKEND",
    "CodexSubscriptionIsolationError",
    "codex_subscription_cli_flags",
    "codex_subscription_cli_overrides",
    "codex_subscription_contract",
    "codex_subscription_exec_canary_command",
    "codex_subscription_isolation_probe_command",
    "codex_subscription_sandbox_smoke_command",
    "codex_subscription_readiness_receipt",
    "validate_codex_subscription_surface",
    "validate_codex_subscription_isolation_result",
    "validate_codex_subscription_sandbox_smoke_result",
    "validate_codex_subscription_version",
]
