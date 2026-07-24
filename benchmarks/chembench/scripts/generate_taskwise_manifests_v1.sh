#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=taskwise_common_v1.sh
source "${SCRIPT_DIR}/taskwise_common_v1.sh"

run_taskwise_manifest_tool generate
