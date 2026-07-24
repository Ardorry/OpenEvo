#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=v2_common.sh
source "${SCRIPT_DIR}/v2_common.sh"

require_bootstrap_python

FRAMEWORK_ROOT="${CHEMBENCH_V2_PACKAGE_ROOT}/state/v2/framework"
FRAMEWORK_LOCK="${FRAMEWORK_ROOT}/framework-lock.json"

if [[ ! -f "${FRAMEWORK_LOCK}" ]]; then
  if [[ -e "${FRAMEWORK_ROOT}" ]]; then
    echo "BLOCKED: partial framework bundle exists" >&2
    exit 2
  fi
  run_v2_cli_bootstrap build-framework
fi

mapfile -t FRAMEWORK_WHEELS < <(find "${FRAMEWORK_ROOT}" -maxdepth 1 -type f -name 'openevo-*.whl' -print)
if [[ "${#FRAMEWORK_WHEELS[@]}" -ne 1 ]]; then
  echo "BLOCKED: framework bundle must contain exactly one OpenEvo wheel" >&2
  exit 2
fi

if [[ ! -x "${CHEMBENCH_V2_RUNTIME_PYTHON}" ]]; then
  if [[ -e "${CHEMBENCH_V2_RUNTIME_ROOT}" ]]; then
    echo "BLOCKED: partial v2 runtime environment exists" >&2
    exit 2
  fi
  "${CHEMBENCH_V2_BOOTSTRAP_PYTHON}" -m venv "${CHEMBENCH_V2_RUNTIME_ROOT}"
  "${CHEMBENCH_V2_RUNTIME_PYTHON}" -m pip \
    --disable-pip-version-check install "${FRAMEWORK_WHEELS[0]}"
  "${CHEMBENCH_V2_RUNTIME_PYTHON}" -m pip \
    --disable-pip-version-check install -e "${CHEMBENCH_V2_PACKAGE_ROOT}"
fi

"${CHEMBENCH_V2_RUNTIME_PYTHON}" -I - <<'PY'
import importlib.metadata as metadata
import json

core = metadata.distribution("openevo")
benchmark = metadata.distribution("openevo-chembench")
direct_url = json.loads(core.read_text("direct_url.json") or "{}")
if "dir_info" in direct_url:
    raise SystemExit("BLOCKED: OpenEvo is editable in the v2 runtime")
print(
    json.dumps(
        {
            "status": "PASS",
            "openevo": core.version,
            "openevo_chembench": benchmark.version,
            "core_editable": False,
        },
        sort_keys=True,
    )
)
PY

run_v2_cli source-manifest
run_v2_cli validate-static
