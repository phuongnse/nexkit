#!/usr/bin/env bash
# The differential check as CI and NexKit's checks run it: a fixed sample of the engine against
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
# Each part that moves a host function to the stored state alone adds its own --forbid.
python3 "$root/tests/differential/harness.py" --v6 "$dir" --sample "${NEXKIT_DIFF_SAMPLE:-400}" \
  --fallbacks --forbid nexkit.host.close_done --forbid nexkit.host.stage_limited \
  --forbid nexkit.host.stage_transient --forbid nexkit.host.resumes --forbid nexkit.host.command \
  --forbid nexkit.host._on_base --forbid nexkit.host.closing_command \
  --forbid nexkit.host.required_failed --forbid nexkit.host.accept_answer \
  --forbid nexkit.host.update_failed --forbid nexkit.host.released \
  --forbid nexkit.host.base_passes --forbid nexkit.host.conflicts_released \
  --forbid nexkit.host.checks_waited \
  --forbid nexkit.host.refusal_cleared --forbid nexkit.host.change_seen \
  --forbid nexkit.host.part_ended --forbid nexkit.host.stopped_counts \
  --forbid nexkit.host.first_plan --forbid nexkit.host.approved \
  --forbid nexkit.host.after_go --forbid nexkit.host.round_carried_on \
  --forbid nexkit.host.no_change_found --forbid nexkit.host.on_base_found \
  --forbid nexkit.host.stage_untrusted --forbid nexkit.host.base_moved
python3 "$root/tests/walk.py" --walks "${NEXKIT_DIFF_WALKS:-1000}"
