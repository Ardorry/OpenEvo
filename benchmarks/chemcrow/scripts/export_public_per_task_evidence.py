#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from openevo_chemcrow.public_evidence import build_public_per_task_evidence


def main() -> int:
    parser = argparse.ArgumentParser(description="Export sanitized ChemCrow per-task evidence.")
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--task-manifest", type=Path, required=True)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--completed-audit", type=Path, required=True)
    parser.add_argument("--aggregate", type=Path, required=True)
    parser.add_argument("--source-code-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--no-model-calls", action="store_true", required=True)
    args = parser.parse_args()
    payload = build_public_per_task_evidence(
        run_root=args.run_root.resolve(),
        task_manifest=args.task_manifest.resolve(),
        experiment_id=args.experiment_id,
        completed_audit=args.completed_audit.resolve(),
        aggregate_path=args.aggregate.resolve(),
        source_code_commit=args.source_code_commit,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": "PASS", "evidence_id": payload["evidence_id"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
