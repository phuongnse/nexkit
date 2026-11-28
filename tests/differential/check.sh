#!/usr/bin/env bash
# The differential check, run by hand: a fixed sample of the engine against
# NexKit 6 at harness.V6, and then the random walks of the engine (tests/walk.py), which need no
# NexKit 6 and stay after 7.0. NEXKIT_V6 names a checkout of that commit to use; otherwise one is
# made in a temporary folder.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
v6=$(sed -n 's/^V6 = "\([0-9a-f]*\)"$/\1/p' "$root/tests/differential/harness.py")
dir=${NEXKIT_V6:-}
if [ -z "$dir" ]; then
  dir=$(mktemp -d)/nexkit-v6
  if git -C "$root" cat-file -e "$v6^{commit}" 2>/dev/null; then
    git -C "$root" worktree add --quiet --detach "$dir" "$v6"
    trap 'git -C "$root" worktree remove --force "$dir"' EXIT
  else
    git clone --quiet --filter=blob:none "$(git -C "$root" remote get-url origin)" "$dir"
    git -C "$dir" checkout --quiet --detach "$v6"
  fi
fi
python3 "$root/tests/differential/harness.py" --v6 "$dir" --sample "${NEXKIT_DIFF_SAMPLE:-400}"
python3 "$root/tests/walk.py" --walks "${NEXKIT_DIFF_WALKS:-1000}"
