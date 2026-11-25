#!/usr/bin/env bash
# Lint as CI and NexKit's checks do: ruff at its pin, then actionlint. Installs either into
# ~/.cache/nexkit-lint when it is missing there.
set -euo pipefail
cd "$(dirname "$0")/.."
tools="$HOME/.cache/nexkit-lint"
if [ ! -x "$tools/venv/bin/ruff" ]; then
  python3 -m venv "$tools/venv"
  "$tools/venv/bin/pip" install --quiet ruff==0.13.3
fi
if [ ! -x "$tools/actionlint" ]; then
  bash <(curl -fsSL https://raw.githubusercontent.com/rhysd/actionlint/v1.7.12/scripts/download-actionlint.bash) 1.7.12 "$tools" >/dev/null
fi
"$tools/venv/bin/ruff" check .
"$tools/venv/bin/ruff" format --check .
"$tools/actionlint" -color
