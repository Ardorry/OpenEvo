#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=v2_common.sh
source "${SCRIPT_DIR}/v2_common.sh"

echo "PAID STEP: exactly 45 dev leave-one-out Codex completions" >&2
run_v2_cli collect-dev-loo
