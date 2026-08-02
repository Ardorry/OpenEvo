#!/usr/bin/env python3
"""Generate the complete aggregate-only final report in one command."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from openevo_chembench.supervised_transfer_v1.common import canonical_pretty_json_bytes
from openevo_chembench.temperature_full_evolve_v1.closure import (
    verify_formal_closure_v1,
    write_formal_closure_receipt_v1,
)
from openevo_chembench.temperature_full_evolve_v1.reporting import (
    load_aggregate_report_input_v1,
    write_final_report_package_v1,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--protocol-source", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--preflight-root", type=Path, required=True)
    parser.add_argument("--closure-receipt-output", type=Path)
    arguments = parser.parse_args()
    input_path = arguments.input.absolute()
    report = load_aggregate_report_input_v1(input_path)
    closure = verify_formal_closure_v1(
        run_root=arguments.run_root.resolve(strict=True),
        preflight_root=arguments.preflight_root.resolve(strict=True),
        aggregate_input_path=input_path,
    )
    closure_path = (
        arguments.closure_receipt_output.absolute()
        if arguments.closure_receipt_output is not None
        else input_path.with_name("formal_closure_receipt_v1.json")
    )
    closure_receipt_sha256 = write_formal_closure_receipt_v1(
        closure,
        path=closure_path,
    )
    result = write_final_report_package_v1(
        report=report,
        closure=closure,
        destination=arguments.destination.absolute(),
        protocol_source=arguments.protocol_source.resolve(strict=True),
    )
    result["formal_closure_receipt_sha256"] = closure_receipt_sha256
    sys.stdout.buffer.write(canonical_pretty_json_bytes(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
