#!/usr/bin/env python3
"""Build the immutable, zero-model-call Temperature formal runtime bundle."""

from __future__ import annotations

import json
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))

from openevo_chembench.temperature_full_evolve_v1.formal_runtime import (
    TemperatureFormalRuntimeError,
    prepare_temperature_formal_runtime_v1,
)


def main() -> int:
    try:
        identity = prepare_temperature_formal_runtime_v1(
            repository_root=REPOSITORY_ROOT.resolve(strict=True),
            bootstrap_python=(REPOSITORY_ROOT / ".venv/bin/python").absolute(),
        )
    except TemperatureFormalRuntimeError as exc:
        print(
            json.dumps(
                {"status": "FAIL_CLOSED", "finding_code": exc.finding_code},
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "status": "PASS",
                "source_commit": identity.receipt["source_commit"],
                "formal_runtime_identity_sha256": identity.digest,
                "formal_runtime_receipt_sha256": identity.receipt_sha256,
                "core_wheel_sha256": identity.receipt["core_wheel_sha256"],
                "chembench_wheel_sha256": identity.receipt[
                    "chembench_wheel_sha256"
                ],
                "framework_registry_digest": identity.receipt[
                    "framework_registry_digest"
                ],
                "model_calls": 0,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
