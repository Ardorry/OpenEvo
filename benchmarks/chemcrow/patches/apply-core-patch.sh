#!/usr/bin/env bash
set -euo pipefail
if [[ $# != 1 ]]; then
  echo "Usage: bash apply-core-patch.sh /path/to/OpenEvo" >&2
  exit 2
fi
patch_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
checkout=$(git -C "$1" rev-parse --show-toplevel)
patch_file="$patch_dir/openevo-session-base-dir.patch"
if git -C "$checkout" apply --reverse --check "$patch_file" 2>/dev/null; then
  echo "Core session_base_dir patch is already present."
  exit 0
fi
expected=4bfe8f797a2026ebbcd95fb64b6ea32646c827dd
if [[ $(git -C "$checkout" rev-parse HEAD) != "$expected" ]]; then
  echo "Refusing unexpected Core revision; expected $expected." >&2
  exit 1
fi
if [[ -n $(git -C "$checkout" status --porcelain -- src/openevo tests/gateway/test_session_workspace_isolation.py) ]]; then
  echo "Refusing to modify a checkout with uncommitted Core changes." >&2
  exit 1
fi
git -C "$checkout" apply --check "$patch_file"
git -C "$checkout" apply "$patch_file"
echo "Applied Core session_base_dir support."
