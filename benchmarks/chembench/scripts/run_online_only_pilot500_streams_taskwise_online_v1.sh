#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=taskwise_common_v1.sh
source "${SCRIPT_DIR}/taskwise_common_v1.sh"

run_taskwise_manifest_tool verify
CONFIG_PATH="$(taskwise_config_path online_pilot500_stream_00_taskwise_online_v1.yaml)"
require_taskwise_source_commit "${CONFIG_PATH}"
TASKWISE_ONLINE_CLI_MODULE=openevo_chembench.taskwise_online_canary_receipt_v1 \
  run_taskwise_cli verify

if [[ "${1:-}" != "--run-id" || -z "${2:-}" ]]; then
  echo "usage: $0 --run-id <fresh-online-only-run-id>" >&2
  exit 2
fi

printf '%s\n' \
  "ONLINE_TASKWISE_EVOLUTION" \
  "TEST_TIME_ADAPTATION" \
  "ONLINE_ONLY_PILOT500" \
  "UNPAIRED_ONLINE_ONLY_NOT_FOR_PAIRED_INFERENCE" \
  "NON_STANDARD_CHEMBENCH4K_PROTOCOL" \
  "NOT_A_STANDARD_LEADERBOARD_SCORE" \
  "DESCRIPTIVE_ONLINE_ONLY_RESULT" \
  "NO_CONTROL_ARM" \
  "NO_CAUSAL_CONTROL_COMPARISON" \
  "NO_PAIRED_PERFORMANCE_CLAIM" >&2
TASKWISE_ONLINE_CLI_MODULE=openevo_chembench.taskwise_online_only_v1 \
  run_taskwise_cli run-pilot500 "$@"
