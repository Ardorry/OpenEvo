#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PACKAGE_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
CONFIG_PATH="${PACKAGE_ROOT}/configs/taskwise_update1_infrastructure_smoke_v1.yaml"

# This bounded infrastructure smoke has no resume path. Every paid invocation
# claims fresh output, Core-state, executor-audit, and reflector-audit roots.
source "${SCRIPT_DIR}/taskwise_common_v1.sh"

COMMAND="run"
if [[ "${1:-}" == "--dry-run" ]]; then
  COMMAND="dry-run"
  shift
fi

TASKWISE_ONLINE_CLI_MODULE="openevo_chembench.taskwise_update1_smoke_v1" \
  run_taskwise_cli "${COMMAND}" --config "${CONFIG_PATH}" "$@"
