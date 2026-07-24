#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=taskwise_common_v1.sh
source "${SCRIPT_DIR}/taskwise_common_v1.sh"

run_taskwise_manifest_tool verify
CONTROL_CONFIG="$(taskwise_config_path control_full_stream_00_taskwise_online_v1.yaml)"
ONLINE_CONFIG="$(taskwise_config_path online_full_stream_00_taskwise_online_v1.yaml)"
require_taskwise_source_commit "${CONTROL_CONFIG}"
require_taskwise_source_commit "${ONLINE_CONFIG}"
run_taskwise_cli verify-pilot-go-receipt
run_taskwise_cli compare-full-streams "$@"
