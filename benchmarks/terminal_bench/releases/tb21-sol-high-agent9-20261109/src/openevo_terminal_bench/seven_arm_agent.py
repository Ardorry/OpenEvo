"""Minimal Harbor Codex adapter for OpenEvo memory and agent-system artifacts."""

from __future__ import annotations

import hashlib
import shlex
from pathlib import Path
from typing import Any, override

from harbor.agents.installed.codex import Codex
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from openevo_terminal_bench.codex_toolchain import (
    ROOT_NAME,
    validate_codex_toolchain_bundle,
)


class OpenEvoSevenArmCodexAgent(Codex):
    """Use Harbor-native skill injection plus audited M/A inputs."""

    def __init__(
        self,
        *args: Any,
        memory_path: str | None = None,
        agent_system_path: str | None = None,
        toolchain_bundle_path: str | None = None,
        toolchain_bundle_sha256: str | None = None,
        ca_bundle_path: str | None = None,
        ca_bundle_sha256: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._memory_path = _optional_file(memory_path, "memory")
        self._agent_system_path = _optional_file(agent_system_path, "agent system")
        self._toolchain_path = _optional_file(toolchain_bundle_path, "toolchain bundle")
        self._toolchain_receipt: dict[str, Any] | None = None
        self._ca_bundle_path = _optional_file(ca_bundle_path, "CA bundle")
        self._ca_bundle_receipt: dict[str, str] | None = None
        if self._toolchain_path is not None:
            if self._version is None:
                raise ValueError("toolchain bundle requires an exact Codex version")
            if toolchain_bundle_sha256 is None:
                raise ValueError("toolchain bundle requires its SHA256")
            self._toolchain_receipt = validate_codex_toolchain_bundle(
                self._toolchain_path,
                expected_version=self._version,
                expected_sha256=toolchain_bundle_sha256,
            )
        elif toolchain_bundle_sha256 is not None:
            raise ValueError("toolchain bundle SHA256 was provided without a bundle")
        if self._ca_bundle_path is not None:
            if ca_bundle_sha256 is None:
                raise ValueError("CA bundle requires its SHA256")
            actual_sha256 = hashlib.sha256(self._ca_bundle_path.read_bytes()).hexdigest()
            if actual_sha256 != ca_bundle_sha256:
                raise ValueError("CA bundle SHA256 mismatch")
            self._ca_bundle_receipt = {
                "path": str(self._ca_bundle_path),
                "sha256": actual_sha256,
            }
        elif ca_bundle_sha256 is not None:
            raise ValueError("CA bundle SHA256 was provided without a bundle")

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        if self._ca_bundle_path is not None and self._ca_bundle_receipt is not None:
            digest = self._ca_bundle_receipt["sha256"]
            remote_bundle = f"/tmp/openevo-ca-bundle-{digest[:16]}.pem"
            await environment.upload_file(self._ca_bundle_path, remote_bundle)
            await self.exec_as_root(
                environment,
                command=(
                    "set -e; "
                    "mkdir -p /etc/ssl/certs; "
                    f"cp {shlex.quote(remote_bundle)} "
                    "/etc/ssl/certs/ca-certificates.crt; "
                    "chmod 0644 /etc/ssl/certs/ca-certificates.crt"
                ),
            )
        if self._toolchain_path is None or self._toolchain_receipt is None:
            await super().install(environment)
            return
        if await self._installed_codex_satisfies_version(environment):
            return
        digest = self._toolchain_receipt["bundle_sha256"]
        remote_archive = f"/tmp/openevo-codex-toolchain-{digest[:16]}.tar.gz"
        install_parent = "/opt"
        install_root = f"{install_parent}/{ROOT_NAME}"
        await environment.upload_file(self._toolchain_path, remote_archive)
        quoted_archive = shlex.quote(remote_archive)
        quoted_parent = shlex.quote(install_parent)
        quoted_root = shlex.quote(install_root)
        await self.exec_as_root(
            environment,
            command=(
                "set -euo pipefail; "
                '[ "$(uname -m)" = "x86_64" ]; '
                f"mkdir -p {quoted_parent}; "
                f"if [ ! -d {quoted_root} ]; then "
                f"tar -xzf {quoted_archive} -C {quoted_parent}; fi; "
                f"cd {quoted_root}; sha256sum -c SHA256SUMS; "
                f"ln -sfn {quoted_root}/bin/codex /usr/local/bin/codex; "
                f"ln -sfn {quoted_root}/vendor/x86_64-unknown-linux-musl/codex-path/rg "
                "/usr/local/bin/rg; "
                "codex --version; rg --version >/dev/null; "
                f"rm -f {quoted_archive}"
            ),
        )

    @override
    async def setup(self, environment: BaseEnvironment) -> None:
        await super().setup(environment)
        if self._agent_system_path is not None:
            await environment.upload_file(self._agent_system_path, "/app/AGENTS.md")

    @override
    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        if self._memory_path is not None:
            memory = self._memory_path.read_text(encoding="utf-8").strip()
            instruction = (
                f"Use the following long-term memory for this task:\n{memory}\n\n"
                f"Task:\n{instruction}"
            )
        await super().run(instruction, environment, context)

    def injection_receipt(self) -> dict[str, dict[str, Any] | None]:
        return {
            "memory": _receipt(self._memory_path),
            "agent_system": _receipt(self._agent_system_path),
            "toolchain": self._toolchain_receipt,
            "ca_bundle": self._ca_bundle_receipt,
        }


def _optional_file(value: str | None, label: str) -> Path | None:
    if value is None:
        return None
    path = Path(value).resolve()
    if not path.is_file():
        raise ValueError(f"{label} artifact is not a file: {path}")
    return path


def _receipt(path: Path | None) -> dict[str, str] | None:
    if path is None:
        return None
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


__all__ = ["OpenEvoSevenArmCodexAgent"]
