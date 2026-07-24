from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openevo_chembench.config import load_debug_config
from openevo_chembench.preflight import (
    PreflightFindingCode,
    run_real_execution_preflight,
)


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PACKAGE_ROOT.parents[1]
CONFIG_PATH = PACKAGE_ROOT / "configs" / "debug_gpt55_evolution.yaml"


def _healthy_rollout(_url: str) -> dict[str, object]:
    return {
        "status": "ok",
        "gateway_registration": {
            "registered": True,
            "schedulable": True,
        },
    }


class RealExecutionPreflightTests(unittest.TestCase):
    def test_current_core_fails_closed_on_model_transport_policy_conflict(self) -> None:
        loaded = load_debug_config(CONFIG_PATH)
        with tempfile.TemporaryDirectory() as temporary:
            auth_file = Path(temporary) / "auth.json"
            auth_file.write_text("{}\n", encoding="utf-8")
            auth_file.chmod(0o600)

            with patch(
                "openevo_chembench.preflight._git_head",
                return_value=loaded.experiment.openevo_revision,
            ):
                receipt = run_real_execution_preflight(
                    config=loaded.experiment,
                    execution=loaded.execution,
                    repository_root=REPOSITORY_ROOT,
                    auth_file=auth_file,
                    docker_probe=lambda: True,
                    managed_runtime_probe=lambda: True,
                    rollout_health_probe=_healthy_rollout,
                )

        self.assertFalse(receipt.passed)
        self.assertEqual(
            receipt.finding_codes,
            (PreflightFindingCode.MODEL_TRANSPORT_POLICY_CONFLICT,),
        )
        self.assertEqual(
            set(receipt.to_audit_payload()),
            {"schema_version", "passed", "finding_codes"},
        )

    def test_unavailable_runtime_is_reported_without_sensitive_evidence(self) -> None:
        loaded = load_debug_config(CONFIG_PATH)
        receipt = run_real_execution_preflight(
            config=loaded.experiment,
            execution=loaded.execution,
            repository_root=REPOSITORY_ROOT,
            auth_file=Path("/definitely/missing/openevo-codex-auth.json"),
            docker_probe=lambda: False,
            rollout_health_probe=lambda _url: None,
        )

        self.assertFalse(receipt.passed)
        self.assertIn(
            PreflightFindingCode.SUBSCRIPTION_AUTH_UNAVAILABLE,
            receipt.finding_codes,
        )
        self.assertIn(PreflightFindingCode.DOCKER_UNAVAILABLE, receipt.finding_codes)
        self.assertIn(
            PreflightFindingCode.ROLLOUT_UNREACHABLE,
            receipt.finding_codes,
        )
        serialized = str(receipt.to_audit_payload())
        self.assertNotIn("auth.json", serialized)
        self.assertNotIn("question", serialized)
        self.assertNotIn("target", serialized)

    def test_missing_managed_runtime_image_fails_before_dataset_access(self) -> None:
        loaded = load_debug_config(CONFIG_PATH)
        with tempfile.TemporaryDirectory() as temporary:
            auth_file = Path(temporary) / "auth.json"
            auth_file.write_text("{}\n", encoding="utf-8")
            auth_file.chmod(0o600)

            receipt = run_real_execution_preflight(
                config=loaded.experiment,
                execution=loaded.execution,
                repository_root=REPOSITORY_ROOT,
                auth_file=auth_file,
                docker_probe=lambda: True,
                managed_runtime_probe=lambda: False,
                rollout_health_probe=_healthy_rollout,
            )

        self.assertIn(
            PreflightFindingCode.MANAGED_RUNTIME_UNAVAILABLE,
            receipt.finding_codes,
        )


if __name__ == "__main__":
    unittest.main()
