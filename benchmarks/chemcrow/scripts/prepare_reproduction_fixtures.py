#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from openevo_chemcrow.reproduction import prepare_or_validate_fixtures


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch and verify pinned ChemCrow fixtures.")
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--destination-root", type=Path, required=True)
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--required-for", action="append", default=[])
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    payload = prepare_or_validate_fixtures(
        lock_path=args.lock.resolve(),
        destination_root=args.destination_root.resolve(),
        check_only=args.check_only,
        required_for=frozenset(args.required_for),
    )
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.receipt:
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
