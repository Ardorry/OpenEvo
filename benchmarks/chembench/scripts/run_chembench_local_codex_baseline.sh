#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
REPOSITORY_ROOT="$(cd -- "${PACKAGE_ROOT}/../.." && pwd)"

exec "${REPOSITORY_ROOT}/.venv/bin/python" \
  "${SCRIPT_DIR}/run_chembench_formal.py" \
  --config "${PACKAGE_ROOT}/configs/local_codex_baseline.yaml" \
  "$@"
