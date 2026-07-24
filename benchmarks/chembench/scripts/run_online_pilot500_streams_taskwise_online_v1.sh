#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=taskwise_common_v1.sh
source "${SCRIPT_DIR}/taskwise_common_v1.sh"

run_taskwise_manifest_tool verify
CONFIG_PATH="$(taskwise_config_path online_pilot500_stream_00_taskwise_online_v1.yaml)"
require_taskwise_source_commit "${CONFIG_PATH}"
run_taskwise_cli verify-canary-receipt
# The suite command constructs a fresh runner and scope-specific Core store.
# Every stream starts once from generation-zero memory in a fresh immutable
# generation namespace; existing stream or suite evidence fails closed.
run_taskwise_cli run-pilot-stream-suite --arm online "$@"
