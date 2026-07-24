#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=taskwise_common_v1.sh
source "${SCRIPT_DIR}/taskwise_common_v1.sh"

CONFIG_PATH="$(taskwise_config_path online_canary9_taskwise_online_v1.yaml)"
run_taskwise_manifest_tool verify
require_taskwise_source_commit "${CONFIG_PATH}"
run_taskwise_cli run-arm --config "${CONFIG_PATH}" "$@"
