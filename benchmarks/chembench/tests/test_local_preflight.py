from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from openevo_chembench.config import load_debug_config
from openevo_chembench.local_preflight import (
    LocalPreflightFindingCode,
    run_local_codex_preflight,
)


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PACKAGE_ROOT.parents[1]
CONFIG_PATH = PACKAGE_ROOT / "configs" / "local_codex_debug.yaml"


class LocalCodexPreflightTests(unittest.TestCase):
    def test_healthy_local_environment_passes_without_dataset_rows(self) -> None:
        loaded = load_debug_config(CONFIG_PATH)
        with tempfile.TemporaryDirectory(dir=REPOSITORY_ROOT) as temporary:
            temporary_root = Path(temporary)
            auth_file = temporary_root / "auth.json"
            auth_file.write_text("{}\n", encoding="utf-8")
            auth_file.chmod(0o600)
            output_target = temporary_root / "results" / "single_task"

            receipt = run_local_codex_preflight(
                config=loaded.experiment,
                execution=loaded.execution,
                repository_root=REPOSITORY_ROOT,
                output_target=output_target,
                auth_file=auth_file,
                codex_version_probe=lambda: "0.144.6",
                codex_login_probe=lambda: True,
                dataset_revision_probe=lambda _repo, _revision: True,
            )

        self.assertTrue(receipt.passed)
        self.assertEqual(receipt.finding_codes, ())

    def test_version_login_dataset_and_existing_output_fail_closed(self) -> None:
        loaded = load_debug_config(CONFIG_PATH)
        with tempfile.TemporaryDirectory(dir=REPOSITORY_ROOT) as temporary:
            temporary_root = Path(temporary)
            auth_file = temporary_root / "auth.json"
            auth_file.write_text("{}\n", encoding="utf-8")
            auth_file.chmod(0o600)
            output_target = temporary_root / "results"
            output_target.mkdir()

            receipt = run_local_codex_preflight(
                config=loaded.experiment,
                execution=loaded.execution,
                repository_root=REPOSITORY_ROOT,
                output_target=output_target,
                auth_file=auth_file,
                codex_version_probe=lambda: "0.1.0",
                codex_login_probe=lambda: False,
                dataset_revision_probe=lambda _repo, _revision: False,
            )

        self.assertFalse(receipt.passed)
        self.assertIn(
            LocalPreflightFindingCode.CODEX_VERSION_MISMATCH,
            receipt.finding_codes,
        )
        self.assertIn(
            LocalPreflightFindingCode.CODEX_LOGIN_UNAVAILABLE,
            receipt.finding_codes,
        )
        self.assertIn(
            LocalPreflightFindingCode.DATASET_REVISION_UNAVAILABLE,
            receipt.finding_codes,
        )
        self.assertIn(
            LocalPreflightFindingCode.OUTPUT_TARGET_EXISTS,
            receipt.finding_codes,
        )
        serialized = str(receipt.to_audit_payload())
        self.assertNotIn("auth.json", serialized)
        self.assertNotIn("question", serialized)


if __name__ == "__main__":
    unittest.main()
