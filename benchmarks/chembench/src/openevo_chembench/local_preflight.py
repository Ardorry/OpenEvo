"""Read-only launch gates for the isolated local Codex CLI backend."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from huggingface_hub import HfApi

from openevo_chembench.config import DebugExecutionConfig, ExperimentConfig
from openevo_chembench.formal_config import FormalExecutionConfig


class LocalPreflightFindingCode(str, Enum):
    """Closed, answer-free local launch-gate vocabulary."""

    OPENEVO_REVISION_MISMATCH = "openevo_revision_mismatch"
    CODEX_UNAVAILABLE = "codex_unavailable"
    CODEX_VERSION_MISMATCH = "codex_version_mismatch"
    CODEX_LOGIN_UNAVAILABLE = "codex_login_unavailable"
    SUBSCRIPTION_AUTH_UNAVAILABLE = "subscription_auth_unavailable"
    SUBSCRIPTION_AUTH_PERMISSIONS_UNSAFE = "subscription_auth_permissions_unsafe"
    DATASET_REVISION_UNAVAILABLE = "dataset_revision_unavailable"
    OUTPUT_TARGET_EXISTS = "output_target_exists"
    OUTPUT_TARGET_UNSAFE = "output_target_unsafe"
    OUTPUT_PARENT_NOT_WRITABLE = "output_parent_not_writable"
    EXECUTION_POLICY_INVALID = "execution_policy_invalid"


@dataclass(frozen=True, slots=True)
class LocalCodexPreflightReceipt:
    """Safe receipt produced without loading any ChemBench example."""

    passed: bool
    finding_codes: tuple[LocalPreflightFindingCode, ...]

    def __post_init__(self) -> None:
        if type(self.passed) is not bool:
            raise TypeError("LocalCodexPreflightReceipt.passed must be boolean")
        if not isinstance(self.finding_codes, tuple) or not all(
            type(code) is LocalPreflightFindingCode for code in self.finding_codes
        ):
            raise TypeError("local preflight finding codes must use the closed enum")
        if len(self.finding_codes) != len(set(self.finding_codes)):
            raise ValueError("local preflight finding codes must be unique")
        if self.passed == bool(self.finding_codes):
            raise ValueError("local preflight decision must agree with findings")

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "passed": self.passed,
            "finding_codes": [code.value for code in self.finding_codes],
        }


CodexVersionProbe = Callable[[], str | None]
CodexLoginProbe = Callable[[], bool]
DatasetRevisionProbe = Callable[[str, str], bool]


def run_local_codex_preflight(
    *,
    config: ExperimentConfig,
    execution: DebugExecutionConfig | FormalExecutionConfig,
    repository_root: Path,
    output_target: Path,
    auth_file: Path | None = None,
    codex_version_probe: CodexVersionProbe | None = None,
    codex_login_probe: CodexLoginProbe | None = None,
    dataset_revision_probe: DatasetRevisionProbe | None = None,
) -> LocalCodexPreflightReceipt:
    """Validate local CLI, auth, dataset pin, policy, and output target."""

    if type(config) is not ExperimentConfig:
        raise TypeError("local preflight config must be exact ExperimentConfig")
    if type(execution) not in {DebugExecutionConfig, FormalExecutionConfig}:
        raise TypeError("local preflight received an invalid execution config")
    if not isinstance(repository_root, Path) or not isinstance(output_target, Path):
        raise TypeError("local preflight paths must be pathlib.Path")
    if auth_file is not None and not isinstance(auth_file, Path):
        raise TypeError("auth_file must be pathlib.Path or None")

    findings: set[LocalPreflightFindingCode] = set()
    if _git_head(repository_root) != config.openevo_revision:
        findings.add(LocalPreflightFindingCode.OPENEVO_REVISION_MISMATCH)
    if not _policy_is_valid(config):
        findings.add(LocalPreflightFindingCode.EXECUTION_POLICY_INVALID)

    resolved_auth = auth_file if auth_file is not None else _default_auth_file()
    try:
        auth_metadata = resolved_auth.stat()
    except OSError:
        findings.add(LocalPreflightFindingCode.SUBSCRIPTION_AUTH_UNAVAILABLE)
    else:
        if not resolved_auth.is_file():
            findings.add(LocalPreflightFindingCode.SUBSCRIPTION_AUTH_UNAVAILABLE)
        elif stat.S_IMODE(auth_metadata.st_mode) & 0o077:
            findings.add(
                LocalPreflightFindingCode.SUBSCRIPTION_AUTH_PERMISSIONS_UNSAFE
            )

    probe_version = (
        _codex_version if codex_version_probe is None else codex_version_probe
    )
    try:
        actual_version = probe_version()
    except Exception:
        actual_version = None
    if actual_version is None:
        findings.add(LocalPreflightFindingCode.CODEX_UNAVAILABLE)
    elif actual_version != config.codex_cli_version:
        findings.add(LocalPreflightFindingCode.CODEX_VERSION_MISMATCH)

    probe_login = _codex_logged_in if codex_login_probe is None else codex_login_probe
    try:
        logged_in = probe_login()
    except Exception:
        logged_in = False
    if logged_in is not True:
        findings.add(LocalPreflightFindingCode.CODEX_LOGIN_UNAVAILABLE)

    probe_dataset = (
        _dataset_revision_available
        if dataset_revision_probe is None
        else dataset_revision_probe
    )
    try:
        dataset_available = probe_dataset(
            config.dataset.repository,
            config.chembench_revision,
        )
    except Exception:
        dataset_available = False
    if dataset_available is not True:
        findings.add(LocalPreflightFindingCode.DATASET_REVISION_UNAVAILABLE)

    _audit_output_target(
        output_target=output_target,
        repository_root=repository_root,
        findings=findings,
    )

    ordered = tuple(sorted(findings, key=lambda code: code.value))
    return LocalCodexPreflightReceipt(
        passed=not ordered,
        finding_codes=ordered,
    )


def _policy_is_valid(config: ExperimentConfig) -> bool:
    runtime = config.runtime
    return (
        config.execution_backend == "local_codex_cli"
        and config.agent.harness == "codex_cli"
        and config.agent.model == "gpt-5.5"
        and config.agent.auth_mode == "subscription"
        and config.agent.capture_mode == "transcript"
        and config.agent.ephemeral is True
        and runtime.network_enabled is False
        and runtime.network_tools_enabled is False
        and runtime.browser_enabled is False
        and runtime.external_web_enabled is False
        and runtime.mcp_servers == ()
        and runtime.private_dataset_mounted is False
    )


def _audit_output_target(
    *,
    output_target: Path,
    repository_root: Path,
    findings: set[LocalPreflightFindingCode],
) -> None:
    root = repository_root.resolve()
    target = output_target.resolve(strict=False)
    if not target.is_relative_to(root) or "results" not in target.relative_to(root).parts:
        findings.add(LocalPreflightFindingCode.OUTPUT_TARGET_UNSAFE)
        return
    if target.exists():
        findings.add(LocalPreflightFindingCode.OUTPUT_TARGET_EXISTS)
        return
    parent = target.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    if not parent.is_dir() or not os.access(parent, os.W_OK | os.X_OK):
        findings.add(LocalPreflightFindingCode.OUTPUT_PARENT_NOT_WRITABLE)


def _default_auth_file() -> Path:
    configured_root = os.environ.get("CODEX_HOME")
    root = (
        Path(configured_root).expanduser()
        if configured_root
        else Path.home() / ".codex"
    )
    return root / "auth.json"


def _git_head(repository_root: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", os.fspath(repository_root), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return value if value else None


def _codex_version() -> str | None:
    executable = shutil.which("codex")
    if executable is None:
        return None
    try:
        completed = subprocess.run(
            [executable, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    prefix = "codex-cli "
    output = completed.stdout.strip()
    if completed.returncode != 0 or not output.startswith(prefix):
        return None
    version = output.removeprefix(prefix)
    return version if version and " " not in version else None


def _codex_logged_in() -> bool:
    executable = shutil.which("codex")
    if executable is None:
        return False
    try:
        completed = subprocess.run(
            [executable, "login", "status"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    outputs = {
        completed.stdout.strip(),
        completed.stderr.strip(),
    }
    return completed.returncode == 0 and outputs == {
        "",
        "Logged in using ChatGPT",
    }


def _dataset_revision_available(repository: str, revision: str) -> bool:
    info = HfApi().dataset_info(
        repo_id=repository,
        revision=revision,
        timeout=15.0,
    )
    return info.sha == revision


__all__ = [
    "LocalCodexPreflightReceipt",
    "LocalPreflightFindingCode",
    "run_local_codex_preflight",
]
