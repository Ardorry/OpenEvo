#!/usr/bin/env python3
"""Emit a no-model, no-dataset-row frozen-v2 protocol gate receipt."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from openevo_chembench.formal_config import load_formal_config
from openevo_chembench.protocol_guard import audit_current_adapter_for_frozen_v2


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PACKAGE_ROOT / "configs" / "local_codex_baseline.yaml"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        loaded = load_formal_config(args.config.resolve())
        receipt = audit_current_adapter_for_frozen_v2(loaded.experiment)
    except Exception as exc:
        _emit(
            {
                "schema_version": 1,
                "status": "audit_error",
                "error_type": type(exc).__name__,
                "paid_execution_allowed": False,
            }
        )
        return 2
    _emit(
        {
            "status": "ready" if receipt.passed else "blocked",
            "dataset_rows_loaded": False,
            "model_calls_made": 0,
            "receipt": receipt.to_audit_payload(),
        }
    )
    return 0 if receipt.passed else 2


def _emit(payload: dict[str, object]) -> None:
    sys.stdout.write(
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n"
    )


if __name__ == "__main__":
    raise SystemExit(main())
