"""Read-only, fail-closed gates for real OpenEvo Codex execution."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import shutil
import stat
import subprocess

import httpx

from openevo.runtime.managed import (
    MANAGED_CODEX_DEFAULT_MODEL,
    MANAGED_CODEX_VERSION,
    MANAGED_RUNTIME_RELEASES,
)

from openevo_chembench.config import DebugExecutionConfig, ExperimentConfig
from openevo_chembench.formal_config import FormalExecutionConfig


class PreflightFindingCode(str, Enum):
    """Closed failure vocabulary for the real-execution launch gate."""

    OPENEVO_REVISION_MISMATCH = "openevo_revision_mismatch"
    MANAGED_CODEX_VERSION_MISMATCH = "managed_codex_version_mismatch"
    MANAGED_MODEL_MISMATCH = "managed_model_mismatch"
    SUBSCRIPTION_AUTH_UNAVAILABLE = "subscription_auth_unavailable"
    SUBSCRIPTION_AUTH_PERMISSIONS_UNSAFE = "subscription_auth_permissions_unsafe"
    DOCKER_UNAVAILABLE = "docker_unavailable"
    MANAGED_RUNTIME_UNAVAILABLE = "managed_runtime_unavailable"
    ROLLOUT_UNREACHABLE = "rollout_unreachable"
    GATEWAY_UNSCHEDULABLE = "gateway_unschedulable"
    MODEL_TRANSPORT_POLICY_CONFLICT = "model_transport_policy_conflict"


@dataclass(frozen=True, slots=True)
class RealExecutionPreflightReceipt:
    """Answer-free receipt produced before any ChemBench task is loaded."""

    passed: bool
    finding_codes: tuple[PreflightFindingCode, ...]

    def __post_init__(self) -> None:
        if type(self.passed) is not bool:
            raise TypeError("RealExecutionPreflightReceipt.passed must be boolean")
        if not isinstance(self.finding_codes, tuple) or not all(
            type(code) is PreflightFindingCode for code in self.finding_codes
        ):
            raise TypeError(
                "RealExecutionPreflightReceipt.finding_codes must be closed finding codes"
            )
        if len(self.finding_codes) != len(set(self.finding_codes)):
            raise ValueError("preflight finding codes must be unique")
        if self.passed == bool(self.finding_codes):
            raise ValueError("preflight decision must agree with finding codes")

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "passed": self.passed,
            "finding_codes": [code.value for code in self.finding_codes],
        }


DockerProbe = Callable[[], bool]
ManagedRuntimeProbe = Callable[[], bool]
RolloutHealthProbe = Callable[[str], Mapping[str, object] | None]


def run_real_execution_preflight(
    *,
    config: ExperimentConfig,
    execution: DebugExecutionConfig | FormalExecutionConfig,
    repository_root: Path,
    auth_file: Path | None = None,
    docker_probe: DockerProbe | None = None,
    managed_runtime_probe: ManagedRuntimeProbe | None = None,
    rollout_health_probe: RolloutHealthProbe | None = None,
) -> RealExecutionPreflightReceipt:
    """Validate launch prerequisites without reading dataset rows or writing results."""

    if type(config) is not ExperimentConfig:
        raise TypeError("preflight config must be exact ExperimentConfig")
    if type(execution) not in {DebugExecutionConfig, FormalExecutionConfig}:
        raise TypeError(
            "preflight execution must be exact DebugExecutionConfig or "
            "FormalExecutionConfig"
        )
    if not isinstance(repository_root, Path):
        raise TypeError("preflight repository_root must be pathlib.Path")
    if auth_file is not None and not isinstance(auth_file, Path):
        raise TypeError("preflight auth_file must be pathlib.Path or None")

    findings: set[PreflightFindingCode] = set()
    if _git_head(repository_root) != config.openevo_revision:
        findings.add(PreflightFindingCode.OPENEVO_REVISION_MISMATCH)
    if MANAGED_CODEX_VERSION != config.codex_cli_version:
        findings.add(PreflightFindingCode.MANAGED_CODEX_VERSION_MISMATCH)
    if MANAGED_CODEX_DEFAULT_MODEL != config.agent.model:
        findings.add(PreflightFindingCode.MANAGED_MODEL_MISMATCH)

    resolved_auth_file = auth_file if auth_file is not None else _default_auth_file()
    try:
        auth_mode = stat.S_IMODE(resolved_auth_file.stat().st_mode)
    except OSError:
        findings.add(PreflightFindingCode.SUBSCRIPTION_AUTH_UNAVAILABLE)
    else:
        if not resolved_auth_file.is_file():
            findings.add(PreflightFindingCode.SUBSCRIPTION_AUTH_UNAVAILABLE)
        elif auth_mode & 0o077:
            findings.add(
                PreflightFindingCode.SUBSCRIPTION_AUTH_PERMISSIONS_UNSAFE
            )

    probe_docker = docker_probe if docker_probe is not None else _docker_available
    try:
        docker_available = probe_docker()
    except Exception:
        docker_available = False
    if docker_available is not True:
        findings.add(PreflightFindingCode.DOCKER_UNAVAILABLE)
    else:
        probe_managed_runtime = (
            managed_runtime_probe
            if managed_runtime_probe is not None
            else _managed_runtime_available
        )
        try:
            managed_runtime_available = probe_managed_runtime()
        except Exception:
            managed_runtime_available = False
        if managed_runtime_available is not True:
            findings.add(PreflightFindingCode.MANAGED_RUNTIME_UNAVAILABLE)

    probe_rollout = (
        rollout_health_probe
        if rollout_health_probe is not None
        else _rollout_health
    )
    try:
        health = probe_rollout(execution.rollout_url)
    except Exception:
        health = None
    if not isinstance(health, Mapping) or health.get("status") != "ok":
        findings.add(PreflightFindingCode.ROLLOUT_UNREACHABLE)
    elif not _gateway_schedulable(health):
        findings.add(PreflightFindingCode.GATEWAY_UNSCHEDULABLE)

    if _model_transport_conflicts_with_required_isolation(config):
        findings.add(PreflightFindingCode.MODEL_TRANSPORT_POLICY_CONFLICT)

    ordered = tuple(sorted(findings, key=lambda code: code.value))
    return RealExecutionPreflightReceipt(
        passed=not ordered,
        finding_codes=ordered,
    )


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


def _default_auth_file() -> Path:
    configured_root = os.environ.get("CODEX_HOME")
    root = (
        Path(configured_root).expanduser()
        if configured_root
        else Path.home() / ".codex"
    )
    return root / "auth.json"


def _docker_available() -> bool:
    executable = shutil.which("docker")
    if executable is None:
        return False
    try:
        completed = subprocess.run(
            [executable, "version", "--format", "{{.Server.Version}}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0 and bool(completed.stdout.strip())


def _managed_runtime_available() -> bool:
    executable = shutil.which("docker")
    if executable is None:
        return False
    release = MANAGED_RUNTIME_RELEASES["managed_science"]
    try:
        completed = subprocess.run(
            [
                executable,
                "image",
                "inspect",
                release.loaded_image_id,
                "--format",
                '{{.Id}} {{index .Config.Labels "io.openevo.managed-runtime"}}',
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return (
        completed.returncode == 0
        and completed.stdout.strip() == f"{release.loaded_image_id} true"
    )


def _rollout_health(rollout_url: str) -> Mapping[str, object] | None:
    try:
        with httpx.Client(
            timeout=httpx.Timeout(3.0, connect=1.0),
            trust_env=False,
        ) as client:
            response = client.get(f"{rollout_url.rstrip('/')}/health")
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _gateway_schedulable(health: Mapping[str, object]) -> bool:
    registration = health.get("gateway_registration")
    return (
        isinstance(registration, Mapping)
        and registration.get("registered") is True
        and registration.get("schedulable") is True
    )


def _model_transport_conflicts_with_required_isolation(
    config: ExperimentConfig,
) -> bool:
    """Return true for the pinned Core's coupled task/model network switch."""

    runtime = config.runtime
    return (
        runtime.network_enabled is False
        and runtime.network_tools_enabled is False
        and runtime.browser_enabled is False
        and runtime.external_web_enabled is False
    )


__all__ = [
    "PreflightFindingCode",
    "RealExecutionPreflightReceipt",
    "run_real_execution_preflight",
]
