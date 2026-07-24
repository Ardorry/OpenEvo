#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=v2_common.sh
source "${SCRIPT_DIR}/v2_common.sh"

if [[ ! -f "${CHEMBENCH_V2_PACKAGE_ROOT}/manifests/v2/benchmark_execution_receipt_v2.json" ]]; then
  run_v2_cli_no_paid generate-manifests
  run_v2_cli_no_paid source-manifest
fi
run_v2_cli_no_paid validate-static
run_v2_cli_no_paid dry-run
