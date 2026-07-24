from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import yaml

from openevo_chembench.artifacts import ArtifactKind
from openevo_chembench.config import (
    CHEMBENCH_CONFIGS,
    AgentConfig,
    ChemBenchDatasetConfig,
    EvolutionConfig,
    ExperimentConfig,
    RuntimeIsolationConfig,
    load_debug_config,
)


class ConfigurationTests(unittest.TestCase):
    def test_default_dataset_selects_all_nine_configs(self) -> None:
        config = ChemBenchDatasetConfig()

        self.assertEqual(config.configurations, CHEMBENCH_CONFIGS)
        self.assertEqual(len(config.configurations), 9)

    def test_unknown_dataset_config_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ChemBenchDatasetConfig(configurations=("not_a_chembench_config",))

    def test_agent_identity_is_fixed_to_codex_and_gpt55(self) -> None:
        self.assertEqual(AgentConfig().harness, "codex")
        self.assertEqual(AgentConfig().model, "gpt-5.5")
        with self.assertRaises(ValueError):
            AgentConfig(model="gpt-5.6")
        with self.assertRaises(ValueError):
            AgentConfig(harness="shell")

    def test_execution_backend_and_harness_must_agree(self) -> None:
        local = ExperimentConfig(
            openevo_revision="a" * 40,
            chembench_revision="b" * 40,
            codex_cli_version="0.144.6",
            prompt_version="chembench-public-prompt.v1",
            execution_backend="local_codex_cli",
            agent=AgentConfig(harness="codex_cli"),
        )

        self.assertEqual(local.execution_backend, "local_codex_cli")
        with self.assertRaises(ValueError):
            ExperimentConfig(
                openevo_revision="a" * 40,
                chembench_revision="b" * 40,
                codex_cli_version="0.144.6",
                prompt_version="chembench-public-prompt.v1",
                execution_backend="local_codex_cli",
            )

    def test_evolution_defaults_enable_only_text_memory(self) -> None:
        evolution = EvolutionConfig()

        self.assertEqual(
            evolution.enabled_artifact_kinds,
            (ArtifactKind.TEXT_MEMORY,),
        )
        self.assertFalse(evolution.enable_skill_bundle)
        self.assertFalse(evolution.enable_agent_system)
        self.assertFalse(evolution.enable_parametric_memory)

    def test_skill_and_agent_system_interfaces_can_be_enabled(self) -> None:
        evolution = EvolutionConfig(
            max_rounds=3,
            enable_text_memory=True,
            enable_skill_bundle=True,
            enable_agent_system=True,
        )

        self.assertEqual(
            evolution.enabled_artifact_kinds,
            (
                ArtifactKind.TEXT_MEMORY,
                ArtifactKind.SKILL_BUNDLE,
                ArtifactKind.AGENT_SYSTEM,
            ),
        )

    def test_parametric_memory_cannot_be_enabled(self) -> None:
        with self.assertRaises(ValueError):
            EvolutionConfig(enable_parametric_memory=True)

    def test_unsafe_runtime_surfaces_cannot_be_enabled(self) -> None:
        for kwargs in (
            {"raw_event_export_enabled": True},
            {"gateway_evaluator_enabled": True},
            {"network_enabled": True},
            {"network_tools_enabled": True},
            {"browser_enabled": True},
            {"external_web_enabled": True},
            {"private_dataset_mounted": True},
            {"mcp_servers": ("untrusted-server",)},
            {"num_samples": 2},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    RuntimeIsolationConfig(**kwargs)

    def test_top_level_config_requires_full_revisions(self) -> None:
        config = ExperimentConfig(
            openevo_revision="a" * 40,
            chembench_revision="b" * 40,
            codex_cli_version="0.144.1",
            prompt_version="chembench-public-prompt.v1",
            seed=42,
        )

        self.assertEqual(config.agent.model, "gpt-5.5")
        self.assertEqual(config.evolution.max_rounds, 0)
        with self.assertRaises(ValueError):
            ExperimentConfig(
                openevo_revision="stable",
                chembench_revision="b" * 40,
                codex_cli_version="0.144.1",
                prompt_version="chembench-public-prompt.v1",
            )

    def test_debug_yaml_is_strict_and_pins_real_agent_identity(self) -> None:
        config_path = (
            Path(__file__).resolve().parents[1]
            / "configs"
            / "debug_gpt55_evolution.yaml"
        )

        loaded = load_debug_config(config_path)

        self.assertEqual(loaded.experiment.agent.harness, "codex")
        self.assertEqual(loaded.experiment.execution_backend, "managed_codex")
        self.assertEqual(loaded.experiment.agent.model, "gpt-5.5")
        self.assertEqual(loaded.experiment.codex_cli_version, "0.144.1")
        self.assertEqual(loaded.experiment.evolution.max_rounds, 1)
        self.assertEqual(
            loaded.experiment.evolution.enabled_artifact_kinds,
            (ArtifactKind.TEXT_MEMORY,),
        )
        self.assertEqual(loaded.execution.task_count, 10)
        self.assertFalse(loaded.experiment.runtime.network_enabled)
        self.assertFalse(loaded.experiment.runtime.network_tools_enabled)
        self.assertFalse(loaded.experiment.runtime.browser_enabled)
        self.assertFalse(loaded.experiment.runtime.external_web_enabled)
        self.assertEqual(loaded.experiment.runtime.mcp_servers, ())

    def test_local_debug_yaml_pins_cli_backend_and_safe_result_root(self) -> None:
        config_path = (
            Path(__file__).resolve().parents[1]
            / "configs"
            / "local_codex_debug.yaml"
        )

        loaded = load_debug_config(config_path)

        self.assertEqual(loaded.experiment.execution_backend, "local_codex_cli")
        self.assertEqual(loaded.experiment.agent.harness, "codex_cli")
        self.assertEqual(loaded.experiment.agent.model, "gpt-5.5")
        self.assertEqual(loaded.experiment.codex_cli_version, "0.144.6")
        self.assertEqual(loaded.execution.result_root, "results/debug_local_codex")

    def test_debug_yaml_rejects_unknown_fields(self) -> None:
        config_path = (
            Path(__file__).resolve().parents[1]
            / "configs"
            / "debug_gpt55_evolution.yaml"
        )
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        payload["agent"]["untrusted_extension"] = "forbidden"

        with tempfile.TemporaryDirectory() as temporary:
            modified = Path(temporary) / "config.yaml"
            modified.write_text(
                yaml.safe_dump(payload, sort_keys=False),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_debug_config(modified)


if __name__ == "__main__":
    unittest.main()
