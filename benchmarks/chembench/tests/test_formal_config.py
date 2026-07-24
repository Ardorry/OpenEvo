from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from openevo_chembench.config import CHEMBENCH_CONFIGS
from openevo_chembench.formal_config import load_formal_config


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
BASELINE_CONFIG = PACKAGE_ROOT / "configs" / "chembench_gpt55_baseline.yaml"
EVOLUTION_CONFIG = PACKAGE_ROOT / "configs" / "chembench_gpt55_evolution.yaml"
LOCAL_BASELINE_CONFIG = PACKAGE_ROOT / "configs" / "local_codex_baseline.yaml"
LOCAL_EVOLUTION_CONFIG = PACKAGE_ROOT / "configs" / "local_codex_evolution.yaml"


class FormalConfigTests(unittest.TestCase):
    def test_baseline_config_is_full_revision_pinned_mode(self) -> None:
        loaded = load_formal_config(BASELINE_CONFIG)

        self.assertEqual(loaded.experiment.dataset.configurations, CHEMBENCH_CONFIGS)
        self.assertEqual(loaded.experiment.evolution.max_rounds, 0)
        self.assertEqual(loaded.experiment.agent.model, "gpt-5.5")
        self.assertEqual(loaded.experiment.agent.harness, "codex")
        self.assertEqual(
            loaded.execution.result_root,
            "results/chembench/baseline",
        )

    def test_evolution_config_enables_only_text_memory(self) -> None:
        loaded = load_formal_config(EVOLUTION_CONFIG)

        self.assertEqual(loaded.experiment.dataset.configurations, CHEMBENCH_CONFIGS)
        self.assertEqual(loaded.experiment.evolution.max_rounds, 1)
        self.assertTrue(loaded.experiment.evolution.enable_text_memory)
        self.assertFalse(loaded.experiment.evolution.enable_skill_bundle)
        self.assertFalse(loaded.experiment.evolution.enable_agent_system)
        self.assertFalse(loaded.experiment.evolution.enable_parametric_memory)
        self.assertFalse(loaded.experiment.runtime.network_enabled)
        self.assertEqual(loaded.experiment.runtime.mcp_servers, ())

    def test_unknown_formal_field_fails_closed(self) -> None:
        source = BASELINE_CONFIG.read_text(encoding="utf-8")
        modified = source.replace(
            "  max_poll_attempts: 900\n",
            "  max_poll_attempts: 900\n  hidden_override: true\n",
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.yaml"
            path.write_text(modified, encoding="utf-8")

            with self.assertRaises(ValueError):
                load_formal_config(path)

    def test_local_formal_configs_are_backend_bound(self) -> None:
        baseline = load_formal_config(LOCAL_BASELINE_CONFIG)
        evolution = load_formal_config(LOCAL_EVOLUTION_CONFIG)

        for loaded in (baseline, evolution):
            self.assertEqual(
                loaded.experiment.execution_backend,
                "local_codex_cli",
            )
            self.assertEqual(loaded.experiment.agent.harness, "codex_cli")
            self.assertEqual(loaded.experiment.agent.model, "gpt-5.5")
            self.assertTrue(
                loaded.execution.result_root.startswith(
                    "results/formal/"
                )
            )


if __name__ == "__main__":
    unittest.main()
