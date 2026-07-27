#!/usr/bin/env python3
"""ChemBench supervised three-target transfer v2 entry point."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))

from openevo_chembench.supervised_transfer_v2.config import load_config_v2
from openevo_chembench.supervised_transfer_v2.prepare import (
    prepare_phase0_v2,
    verify_phase0_v2,
)

DEFAULT_CONFIG = (
    PACKAGE_ROOT
    / "configs/supervised_transfer_v2/chembench_supervised_transfer_v2.yaml"
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("command", choices=("prepare", "verify"))
    arguments = parser.parse_args()
    config = load_config_v2(arguments.config.resolve(strict=True))
    payload = (
        prepare_phase0_v2(REPOSITORY_ROOT)
        if arguments.command == "prepare"
        else verify_phase0_v2(REPOSITORY_ROOT)
    )
    payload["config_sha256"] = config.digest
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
