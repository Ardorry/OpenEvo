from __future__ import annotations

import subprocess
from pathlib import Path

from openevo_chembench.v2_config import PROTOCOL_ID, load_frozen_config_v2


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PACKAGE_ROOT.parents[1]
_FROZEN_IMPLEMENTATION_PATHS = (
    "benchmarks/chembench/src/openevo_chembench/frozen_runner_v2.py",
    "benchmarks/chembench/src/openevo_chembench/frozen_runtime_v2.py",
    "benchmarks/chembench/src/openevo_chembench/sampling_v2.py",
    "benchmarks/chembench/src/openevo_chembench/v2_cli.py",
    "benchmarks/chembench/src/openevo_chembench/v2_config.py",
    "benchmarks/chembench/manifests/v2",
)


def test_frozen_generalization_protocol_files_are_unchanged_from_head() -> None:
    completed = subprocess.run(
        ("git", "diff", "--quiet", "HEAD", "--", *_FROZEN_IMPLEMENTATION_PATHS),
        cwd=REPOSITORY_ROOT,
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr


def test_frozen_configs_remain_one_completion_offline_comparators() -> None:
    for scope in ("canary18", "pilot500", "full"):
        baseline = load_frozen_config_v2(
            PACKAGE_ROOT / "configs" / f"baseline_{scope}_frozen_v2.yaml"
        )
        evolved = load_frozen_config_v2(
            PACKAGE_ROOT / "configs" / f"evolved_{scope}_frozen_v2.yaml"
        )

        assert baseline.protocol_id == evolved.protocol_id == PROTOCOL_ID
        assert baseline.protocol_id == "chembench4k_frozen_generalization_v2"
        assert baseline.arm == "baseline"
        assert evolved.arm == "evolved"
        assert (
            baseline.executor.infrastructure_retries_before_completion
            == evolved.executor.infrastructure_retries_before_completion
            == 0
        )
        assert baseline.artifact.enabled is False
        assert evolved.artifact.enabled is True
