from __future__ import annotations

import ast
import subprocess
import sys
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PACKAGE_ROOT.parents[1]
SOURCE_ROOT = PACKAGE_ROOT / "src" / "openevo_chembench"
CORE_ROOT = REPOSITORY_ROOT / "src" / "openevo"


class PackageBoundaryTests(unittest.TestCase):
    def test_benchmark_does_not_vendor_openevo_core(self) -> None:
        self.assertFalse((PACKAGE_ROOT / "src" / "openevo").exists())
        self.assertTrue(SOURCE_ROOT.is_dir())

    def test_core_does_not_import_benchmark_package(self) -> None:
        offenders: list[str] = []
        for path in CORE_ROOT.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ImportFrom)
                    and node.module is not None
                    and node.module.startswith("openevo_chembench")
                ):
                    offenders.append(str(path.relative_to(REPOSITORY_ROOT)))
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith("openevo_chembench"):
                            offenders.append(str(path.relative_to(REPOSITORY_ROOT)))

        self.assertEqual(offenders, [])

    def test_package_modules_match_closed_boundary(self) -> None:
        expected = {
            "__init__.py",
            "agent_executor.py",
            "artifact_validator_v2.py",
            "artifacts.py",
            "benchmark_receipt_v2.py",
            "chembench4k_dataset.py",
            "chembench4k_evaluation.py",
            "chembench4k_models.py",
            "chembench4k_prompt.py",
            "config.py",
            "core_evolution_v2.py",
            "dataset.py",
            "dev_trajectory_v2.py",
            "evaluator.py",
            "feedback.py",
            "formal_config.py",
            "frozen_runner_v2.py",
            "frozen_runtime_v2.py",
            "local_codex_executor.py",
            "local_preflight.py",
            "models.py",
            "paired_statistics_v2.py",
            "preflight.py",
            "prompt_adapter.py",
            "protocol_guard.py",
            "reflector.py",
            "reflector_execution_boundary_v2.py",
            "reporting.py",
            "runner.py",
            "runtime_context.py",
            "sampling_v2.py",
            "security.py",
            "source_identity_v2.py",
            "taskwise_cli_v1.py",
            "taskwise_config_v1.py",
            "taskwise_context_binding_v1.py",
            "taskwise_core_evolution_v1.py",
            "taskwise_feedback_v1.py",
            "taskwise_online_runner_v1.py",
            "taskwise_reporting_v1.py",
            "taskwise_round0_smoke_v1.py",
            "taskwise_sampling_v1.py",
            "taskwise_stream_statistics_v1.py",
            "taskwise_trajectory_v1.py",
            "v2_cli.py",
            "v2_config.py",
        }

        self.assertEqual({path.name for path in SOURCE_ROOT.glob("*.py")}, expected)

    def test_v2_import_does_not_initialize_legacy_loader_or_reflector(self) -> None:
        command = (
            "import json,sys;"
            "import openevo_chembench.chembench4k_dataset;"
            "print(json.dumps(sorted(name for name in sys.modules "
            "if name in {'openevo_chembench.dataset',"
            "'openevo_chembench.reflector',"
            "'openevo_chembench.runner'})))"
        )
        completed = subprocess.run(
            (sys.executable, "-I", "-c", command),
            cwd=PACKAGE_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=30,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "[]")


if __name__ == "__main__":
    unittest.main()
