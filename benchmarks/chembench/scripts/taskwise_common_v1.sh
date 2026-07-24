#!/usr/bin/env bash
set -euo pipefail

TASKWISE_SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
TASKWISE_PACKAGE_ROOT="$(cd -- "${TASKWISE_SCRIPT_DIR}/.." && pwd -P)"
TASKWISE_REPOSITORY_ROOT="$(cd -- "${TASKWISE_PACKAGE_ROOT}/../.." && pwd -P)"
TASKWISE_BOOTSTRAP_PYTHON="${TASKWISE_REPOSITORY_ROOT}/.venv/bin/python"
TASKWISE_RUNTIME_PYTHON="${TASKWISE_PACKAGE_ROOT}/state/v2/runtime_venv/bin/python"

require_taskwise_python() {
  if [[ ! -x "${TASKWISE_BOOTSTRAP_PYTHON}" ]]; then
    echo "BLOCKED: repository .venv Python is unavailable" >&2
    exit 2
  fi
}

run_taskwise_manifest_tool() {
  require_taskwise_python
  (
    cd -- "${TASKWISE_REPOSITORY_ROOT}"
    PYTHONPATH="${TASKWISE_PACKAGE_ROOT}/src" \
      "${TASKWISE_BOOTSTRAP_PYTHON}" -s \
      "${TASKWISE_SCRIPT_DIR}/generate_taskwise_manifests_v1.py" "$@"
  )
}

taskwise_config_path() {
  printf '%s/configs/%s\n' "${TASKWISE_PACKAGE_ROOT}" "$1"
}

require_taskwise_source_commit() {
  require_taskwise_python
  local config_path="$1"
  (
    cd -- "${TASKWISE_REPOSITORY_ROOT}"
    PYTHONPATH="${TASKWISE_PACKAGE_ROOT}/src" \
      "${TASKWISE_BOOTSTRAP_PYTHON}" -s - "${config_path}" <<'PY'
from pathlib import Path
import sys

from openevo_chembench.taskwise_config_v1 import load_taskwise_config_v1
from openevo_chembench.taskwise_cli_v1 import (
    TaskwiseCLIError,
    verify_taskwise_source_gate_v1,
)

config = load_taskwise_config_v1(Path(sys.argv[1]))
try:
    source_commit = verify_taskwise_source_gate_v1(config)
except TaskwiseCLIError as exc:
    raise SystemExit(f"BLOCKED: {exc}") from None
print(f"taskwise source commit gate: PASS ({source_commit})")
PY
  )
}

run_taskwise_cli() {
  if [[ ! -x "${TASKWISE_RUNTIME_PYTHON}" ]]; then
    echo "BLOCKED: run prepare_chembench4k_v2_runtime.sh first" >&2
    exit 2
  fi
  local module="${TASKWISE_ONLINE_CLI_MODULE:-openevo_chembench.taskwise_cli_v1}"
  (
    cd -- "${TASKWISE_REPOSITORY_ROOT}"
    if ! "${TASKWISE_RUNTIME_PYTHON}" -I -c \
      "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('${module}') else 1)"; then
      echo "BLOCKED: TASKWISE_ONLINE_CLI_NOT_INSTALLED (${module})" >&2
      exit 2
    fi
    "${TASKWISE_RUNTIME_PYTHON}" -I -m "${module}" "$@"
  )
}
