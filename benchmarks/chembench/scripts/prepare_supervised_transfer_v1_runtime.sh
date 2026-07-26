#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
workspace_root="$(cd -- "${script_dir}/../../.." && pwd -P)"
bootstrap_python="${workspace_root}/.venv/bin/python"
state_root="${workspace_root}/state/chembench_supervised_transfer_v1"
framework_root="${state_root}/framework"
runtime_root="${state_root}/runtime_venv"
runtime_python="${runtime_root}/bin/python"

if [[ ! -x "${bootstrap_python}" ]]; then
  echo "BLOCKED: repository bootstrap Python is unavailable" >&2
  exit 2
fi

if [[ ! -f "${framework_root}/framework-lock.json" ]]; then
  if [[ -e "${framework_root}" ]]; then
    echo "BLOCKED: partial supervised framework bundle exists" >&2
    exit 2
  fi
  "${bootstrap_python}" - <<'PY'
from pathlib import Path

from openevo_chembench.core_evolution_v2 import build_maintainer_framework_bundle_v2

repository = Path("/home/lhy-h/work/compare2")
build_maintainer_framework_bundle_v2(
    repository_root=repository,
    output_directory=repository / "state/chembench_supervised_transfer_v1/framework",
    python_executable=repository / ".venv/bin/python",
)
PY
fi

mapfile -t framework_wheels < <(
  find "${framework_root}" -maxdepth 1 -type f -name 'openevo-*.whl' -print
)
if [[ "${#framework_wheels[@]}" -ne 1 ]]; then
  echo "BLOCKED: framework bundle must contain exactly one OpenEvo wheel" >&2
  exit 2
fi

if [[ ! -x "${runtime_python}" ]]; then
  if [[ -e "${runtime_root}" ]]; then
    echo "BLOCKED: partial supervised runtime environment exists" >&2
    exit 2
  fi
  "${bootstrap_python}" -m venv "${runtime_root}"
  "${runtime_python}" -m pip --disable-pip-version-check install "${framework_wheels[0]}"
  "${runtime_python}" -m pip --disable-pip-version-check install \
    -e "${workspace_root}/benchmarks/chembench"
fi

"${runtime_python}" -I - "${framework_root}/framework-lock.json" <<'PY'
import importlib.metadata as metadata
import json
import sys
from pathlib import Path

from openevo.evolution.framework import load_verified_framework_registry

core = metadata.distribution("openevo")
benchmark = metadata.distribution("openevo-chembench")
direct_url = json.loads(core.read_text("direct_url.json") or "{}")
if "dir_info" in direct_url:
    raise SystemExit("BLOCKED: OpenEvo is editable in the supervised runtime")
registry = load_verified_framework_registry(Path(sys.argv[1]))
print(
    json.dumps(
        {
            "status": "PASS",
            "openevo": core.version,
            "openevo_chembench": benchmark.version,
            "core_editable": False,
            "registry_digest": registry.snapshot.registry_digest,
        },
        sort_keys=True,
    )
)
PY
