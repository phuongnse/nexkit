#!/usr/bin/env bash
# Lint as CI and NexKit's checks do: ruff at its pin, then actionlint. Installs either into
# ~/.cache/nexkit-lint when it is missing there. The two pins are this repository's own tool
# versions, which D9 leaves here.
set -euo pipefail
cd "$(dirname "$0")/.."
RUFF_VERSION=0.13.3
ACTIONLINT_VERSION=1.7.12
tools="$HOME/.cache/nexkit-lint"
if [ ! -x "$tools/venv/bin/ruff" ]; then
  python3 -m venv "$tools/venv"
  "$tools/venv/bin/pip" install --quiet ruff=="$RUFF_VERSION"
fi
if [ ! -x "$tools/actionlint" ]; then
  bash <(curl -fsSL https://raw.githubusercontent.com/rhysd/actionlint/v$ACTIONLINT_VERSION/scripts/download-actionlint.bash) "$ACTIONLINT_VERSION" "$tools" >/dev/null
fi
"$tools/venv/bin/ruff" check .
"$tools/venv/bin/ruff" format --check .
"$tools/actionlint" -color
