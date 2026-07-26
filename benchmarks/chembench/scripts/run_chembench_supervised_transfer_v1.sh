#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
workspace_root="$(cd -- "${script_dir}/../../.." && pwd)"
bootstrap_python="${workspace_root}/.venv/bin/python"
runtime_python="${workspace_root}/state/chembench_supervised_transfer_v1/runtime_venv/bin/python"
python_executable="${bootstrap_python}"
if [[ "${1:-}" == "run-preflight" || \
      "${1:-}" == "verify-preflight" || \
      "${1:-}" == "run-formal" ]]; then
  if [[ ! -x "${runtime_python}" ]]; then
    echo "BLOCKED: run requires prepare_supervised_transfer_v1_runtime.sh" >&2
    exit 2
  fi
  python_executable="${runtime_python}"
elif [[ ! -x "${python_executable}" ]]; then
  python_executable="python3"
fi

exec "${python_executable}" "${script_dir}/supervised_transfer_v1.py" \
  --config "${workspace_root}/benchmarks/chembench/configs/chembench_supervised_transfer_v1.yaml" \
  "$@"
