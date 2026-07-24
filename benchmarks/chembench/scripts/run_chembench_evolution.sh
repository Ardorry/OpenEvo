#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
REPOSITORY_ROOT="$(cd -- "${PACKAGE_ROOT}/../.." && pwd)"

"${SCRIPT_DIR}/manage_openevo_services.sh" start
exec "${REPOSITORY_ROOT}/.venv/bin/python" \
  "${SCRIPT_DIR}/run_chembench_formal.py" \
  --config "${PACKAGE_ROOT}/configs/chembench_gpt55_evolution.yaml" \
  "$@"
