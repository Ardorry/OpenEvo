#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=v2_common.sh
source "${SCRIPT_DIR}/v2_common.sh"

run_v2_cli compare --scope canary18
