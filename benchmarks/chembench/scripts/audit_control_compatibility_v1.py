#!/usr/bin/env python3
"""Generate a private, content-free Control500 compatibility receipt."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


SCRIPT = Path(__file__).resolve()
REPOSITORY_ROOT = SCRIPT.parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT / "benchmarks" / "chembench" / "src"))

from openevo_chembench.taskwise_control_compatibility_v1 import (  # noqa: E402
    audit_control_run_compatibility_v1,
    write_control_compatibility_receipt_v1,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--generation-id", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    receipt = audit_control_run_compatibility_v1(
        REPOSITORY_ROOT,
        generation_id=args.generation_id,
        attempt_id=args.attempt_id,
    )
    write_control_compatibility_receipt_v1(args.output.resolve(), receipt)
    print(
        " ".join(
            (
                f"status={receipt.status}",
                f"finding_count={len(receipt.finding_codes)}",
                f"completed_streams={receipt.completed_streams}",
                f"completed_tasks={receipt.completed_tasks}",
                f"completed_sessions={receipt.completed_sessions}",
                f"digest={receipt.digest}",
            )
        )
    )
    return 0 if receipt.status == "COMPATIBLE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
