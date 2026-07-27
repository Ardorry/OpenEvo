#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
workspace_root="$(cd -- "${script_dir}/../../.." && pwd -P)"
bootstrap_python="${workspace_root}/.venv/bin/python"
state_root="${workspace_root}/state/chembench_supervised_transfer_v1"
framework_root="${state_root}/framework"
runtime_root="${state_root}/runtime_venv"
runtime_python="${runtime_root}/bin/python"
managed_codex_root="${state_root}/managed_codex"
managed_codex_receipt="${managed_codex_root}/managed_codex_receipt_v1.json"

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

managed_codex_package="$(
  "${runtime_python}" -I - <<'PY'
from openevo.runtime.managed import MANAGED_CODEX_NPM_PACKAGE

print(MANAGED_CODEX_NPM_PACKAGE)
PY
)"

if [[ ! -e "${managed_codex_root}" ]]; then
  npm_executable="$(command -v npm || true)"
  if [[ -z "${npm_executable}" ]]; then
    echo "BLOCKED: npm is required to materialize OpenEvo-managed Codex" >&2
    exit 2
  fi
  install_root="$(mktemp -d "${state_root}/.managed-codex-install.XXXXXX")"
  cache_root="$(mktemp -d "${state_root}/.managed-codex-cache.XXXXXX")"
  cleanup_managed_codex_install() {
    if [[ -n "${install_root:-}" && -d "${install_root}" ]]; then
      rm -rf -- "${install_root}"
    fi
    if [[ -n "${cache_root:-}" && -d "${cache_root}" ]]; then
      rm -rf -- "${cache_root}"
    fi
  }
  trap cleanup_managed_codex_install EXIT HUP INT TERM
  "${npm_executable}" install --global --prefix "${install_root}" \
    --cache "${cache_root}" --ignore-scripts --no-audit --no-fund \
    "${managed_codex_package}"
  chmod -R go-w "${install_root}"
  chmod 0700 "${install_root}"
  mv -- "${install_root}" "${managed_codex_root}"
  install_root=""
  rm -rf -- "${cache_root}"
  cache_root=""
  trap - EXIT HUP INT TERM
fi

"${runtime_python}" -I - "${workspace_root}" "${managed_codex_receipt}" <<'PY'
import json
import sys
from pathlib import Path

from openevo_chembench.supervised_transfer_v1.managed_codex import (
    load_managed_codex_v1,
    write_managed_codex_receipt_v1,
)

repository = Path(sys.argv[1]).resolve(strict=True)
receipt = Path(sys.argv[2])
if not receipt.exists():
    write_managed_codex_receipt_v1(repository_root=repository)
identity = load_managed_codex_v1(repository_root=repository)
print(
    json.dumps(
        {
            "status": "PASS",
            "source": identity.source,
            "npm_package": identity.npm_package,
            "codex_cli_version": identity.codex_cli_version,
            "executable_sha256": identity.executable_sha256,
            "identity_sha256": identity.digest,
        },
        sort_keys=True,
    )
)
PY

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
