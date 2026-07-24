#!/usr/bin/env bash
set -euo pipefail

CHEMBENCH_V2_SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
CHEMBENCH_V2_PACKAGE_ROOT="$(cd -- "${CHEMBENCH_V2_SCRIPT_DIR}/.." && pwd -P)"
CHEMBENCH_V2_REPOSITORY_ROOT="$(cd -- "${CHEMBENCH_V2_PACKAGE_ROOT}/../.." && pwd -P)"
CHEMBENCH_V2_WORKSPACE_ROOT="$(cd -- "${CHEMBENCH_V2_REPOSITORY_ROOT}/.." && pwd -P)"
CHEMBENCH_V2_BOOTSTRAP_PYTHON="${CHEMBENCH_V2_REPOSITORY_ROOT}/.venv/bin/python"
CHEMBENCH_V2_RUNTIME_ROOT="${CHEMBENCH_V2_PACKAGE_ROOT}/state/v2/runtime_venv"
CHEMBENCH_V2_RUNTIME_PYTHON="${CHEMBENCH_V2_RUNTIME_ROOT}/bin/python"

export PYTHONNOUSERSITE=1

require_bootstrap_python() {
  if [[ ! -x "${CHEMBENCH_V2_BOOTSTRAP_PYTHON}" ]]; then
    echo "BLOCKED: repository .venv Python is unavailable" >&2
    exit 2
  fi
}

require_v2_runtime() {
  if [[ ! -x "${CHEMBENCH_V2_RUNTIME_PYTHON}" ]]; then
    echo "BLOCKED: run prepare_chembench4k_v2_runtime.sh first" >&2
    exit 2
  fi
}

run_v2_cli() {
  require_v2_runtime
  (
    cd -- "${CHEMBENCH_V2_REPOSITORY_ROOT}"
    "${CHEMBENCH_V2_RUNTIME_PYTHON}" -I -m openevo_chembench.v2_cli "$@"
  )
}

run_v2_cli_bootstrap() {
  require_bootstrap_python
  (
    cd -- "${CHEMBENCH_V2_REPOSITORY_ROOT}"
    PYTHONPATH="${CHEMBENCH_V2_PACKAGE_ROOT}/src" \
      "${CHEMBENCH_V2_BOOTSTRAP_PYTHON}" -s -m openevo_chembench.v2_cli "$@"
  )
}

run_v2_cli_no_paid() {
  if [[ -x "${CHEMBENCH_V2_RUNTIME_PYTHON}" ]]; then
    run_v2_cli "$@"
  else
    run_v2_cli_bootstrap "$@"
  fi
}

config_path() {
  printf '%s/configs/%s\n' "${CHEMBENCH_V2_PACKAGE_ROOT}" "$1"
}
