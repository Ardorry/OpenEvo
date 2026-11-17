from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest
from harbor.agents.installed.codex import Codex

from openevo_terminal_bench.seven_arm_agent import OpenEvoSevenArmCodexAgent


def test_memory_extends_instruction_without_hiding_task(monkeypatch, tmp_path: Path) -> None:
    memory = tmp_path / "memory.md"
    memory.write_text("Check outputs.", encoding="utf-8")
    captured = {}

    async def fake_run(self, instruction, environment, context):
        captured["instruction"] = instruction

    monkeypatch.setattr(Codex, "run", fake_run)
    agent = OpenEvoSevenArmCodexAgent(
        logs_dir=tmp_path / "logs", model_name="gpt-5.6-terra",
        memory_path=str(memory), reasoning_effort="medium",
    )
    asyncio.run(agent.run("Original task", object(), object()))
    assert "Check outputs." in captured["instruction"]
    assert captured["instruction"].endswith("Task:\nOriginal task")
    assert agent._resolved_flags["reasoning_effort"] == "medium"


def test_agent_system_is_uploaded_to_native_codex_target(monkeypatch, tmp_path: Path) -> None:
    agents = tmp_path / "AGENTS.md"
    agents.write_text("Verify first.", encoding="utf-8")
    uploads = []

    async def fake_setup(self, environment):
        return None

    class Environment:
        async def upload_file(self, source, target):
            uploads.append((Path(source), target))

    monkeypatch.setattr(Codex, "setup", fake_setup)
    agent = OpenEvoSevenArmCodexAgent(
        logs_dir=tmp_path / "logs", model_name="gpt-5.6-terra",
        agent_system_path=str(agents),
    )
    asyncio.run(agent.setup(Environment()))
    assert uploads == [(agents.resolve(), "/app/AGENTS.md")]
    assert agent.injection_receipt()["agent_system"]["sha256"]


def test_missing_artifact_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a file"):
        OpenEvoSevenArmCodexAgent(
            logs_dir=tmp_path / "logs", model_name="gpt-5.6-terra",
            memory_path=str(tmp_path / "missing.md"),
        )


def test_msa_paths_compose_without_disabling_native_skill_registration(
    monkeypatch, tmp_path: Path
) -> None:
    memory = tmp_path / "memory.md"
    memory.write_text("Remember verifier evidence.", encoding="utf-8")
    agents = tmp_path / "AGENTS.md"
    agents.write_text("Validate before finishing.", encoding="utf-8")
    captured = {"uploads": []}

    async def fake_setup(self, environment):
        captured["setup"] = True

    async def fake_run(self, instruction, environment, context):
        captured["instruction"] = instruction

    class Environment:
        async def upload_file(self, source, target):
            captured["uploads"].append((Path(source), target))

    monkeypatch.setattr(Codex, "setup", fake_setup)
    monkeypatch.setattr(Codex, "run", fake_run)
    agent = OpenEvoSevenArmCodexAgent(
        logs_dir=tmp_path / "logs",
        model_name="gpt-5.6-terra",
        memory_path=str(memory),
        agent_system_path=str(agents),
        skills_dir="/harbor/skills",
        reasoning_effort="medium",
    )
    asyncio.run(agent.setup(Environment()))
    asyncio.run(agent.run("Original task", Environment(), object()))
    assert captured["uploads"] == [(agents.resolve(), "/app/AGENTS.md")]
    assert "Remember verifier evidence." in captured["instruction"]
    assert "/harbor/skills" in agent._build_register_skills_command()


def test_g1_agent_has_no_evolution_inputs(tmp_path: Path) -> None:
    agent = OpenEvoSevenArmCodexAgent(
        logs_dir=tmp_path / "logs",
        model_name="gpt-5.6-terra",
        reasoning_effort="medium",
    )
    assert agent.injection_receipt() == {
        "memory": None,
        "agent_system": None,
        "toolchain": None,
        "ca_bundle": None,
    }
    assert agent.skills_dir is None


def test_offline_toolchain_skips_network_installer(
    monkeypatch, tmp_path: Path
) -> None:
    bundle = tmp_path / "toolchain.tar.gz"
    bundle.write_bytes(b"bundle")
    ca_bundle = tmp_path / "ca-certificates.pem"
    ca_bundle.write_bytes(b"test CA bundle")
    ca_digest = hashlib.sha256(ca_bundle.read_bytes()).hexdigest()
    digest = "a" * 64
    monkeypatch.setattr(
        "openevo_terminal_bench.seven_arm_agent.validate_codex_toolchain_bundle",
        lambda *args, **kwargs: {
            "version": "0.144.1",
            "bundle_sha256": digest,
        },
    )

    class Result:
        return_code = 1
        stdout = ""
        stderr = ""

    class Environment:
        def __init__(self):
            self.uploads = []
            self.commands = []

        async def upload_file(self, source, target):
            self.uploads.append((Path(source), target))

        async def exec(self, command, **kwargs):
            self.commands.append(command)
            if "sha256sum -c" in command or "ca-certificates.crt" in command:
                result = Result()
                result.return_code = 0
                return result
            return Result()

    environment = Environment()
    agent = OpenEvoSevenArmCodexAgent(
        logs_dir=tmp_path / "logs",
        model_name="gpt-5.6-terra",
        version="0.144.1",
        toolchain_bundle_path=str(bundle),
        toolchain_bundle_sha256=digest,
        ca_bundle_path=str(ca_bundle),
        ca_bundle_sha256=ca_digest,
    )
    asyncio.run(agent.install(environment))
    assert environment.uploads == [
        (ca_bundle.resolve(), f"/tmp/openevo-ca-bundle-{ca_digest[:16]}.pem"),
        (bundle.resolve(), f"/tmp/openevo-codex-toolchain-{digest[:16]}.tar.gz")
    ]
    rendered = "\n".join(environment.commands)
    assert "cp /tmp/openevo-ca-bundle-" in rendered
    assert "/etc/ssl/certs/ca-certificates.crt" in rendered
    assert "sha256sum -c SHA256SUMS" in rendered
    assert "apt-get" not in rendered
    assert "npm install" not in rendered
    assert "nvm install" not in rendered
