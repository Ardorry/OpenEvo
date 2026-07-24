#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=taskwise_common_v1.sh
source "${SCRIPT_DIR}/taskwise_common_v1.sh"

run_taskwise_manifest_tool verify
CONFIG_PATH="$(taskwise_config_path control_full_stream_00_taskwise_online_v1.yaml)"
require_taskwise_source_commit "${CONFIG_PATH}"
# This recomputes pilot GO and every full config/manifest hash.  No caller
# boolean or prior process exit code can authorize paid full execution.
run_taskwise_cli verify-pilot-go-receipt
run_taskwise_cli run-full-stream-suite --arm control "$@"
