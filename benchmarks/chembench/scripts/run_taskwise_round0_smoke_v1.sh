#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PACKAGE_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
CONFIG_PATH="${PACKAGE_ROOT}/configs/taskwise_round0_infrastructure_smoke_v1.yaml"

# This entrypoint has no resume mode. Every invocation must claim a new run ID,
# and the Python controller rejects both output and diagnostic namespace reuse.
source "${SCRIPT_DIR}/taskwise_common_v1.sh"

COMMAND="run"
if [[ "${1:-}" == "--dry-run" ]]; then
  COMMAND="dry-run"
  shift
elif [[ "${1:-}" == "--prepare-manifests" ]]; then
  COMMAND="prepare-manifests"
  shift
fi

TASKWISE_ONLINE_CLI_MODULE="openevo_chembench.taskwise_round0_smoke_v1" \
  run_taskwise_cli "${COMMAND}" --config "${CONFIG_PATH}" "$@"
