#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from openevo_chemcrow.public_evidence import build_public_paper_judge_evidence


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export sanitized ChemCrow paper-Judge per-task evidence."
    )
    parser.add_argument("--results-root", type=Path, required=True)
    parser.add_argument("--aggregate", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--no-model-calls", action="store_true", required=True)
    args = parser.parse_args()
    payload = build_public_paper_judge_evidence(
        results_root=args.results_root.resolve(),
        aggregate_path=args.aggregate.resolve(),
        run_id=args.run_id,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": "PASS", "evidence_id": payload["evidence_id"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
