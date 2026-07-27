#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repository_root="$(cd -- "${script_dir}/../../../.." && pwd)"

exec "${repository_root}/.venv/bin/python" "${script_dir}/main.py" "$@"
