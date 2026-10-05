#!/usr/bin/env python3
"""Show or update the one Claude Code version NexKit supports.

    claude_code_version.py current        print the pinned version
    claude_code_version.py stable         print the version on Claude Code's stable channel
    claude_code_version.py set VERSION    pin VERSION
    claude_code_version.py update         pin the stable version if it is newer; print it

The pin lives only in nexkit/__init__.py. The pipeline and CI read it from there.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
INIT = ROOT / "nexkit" / "__init__.py"
CHANNEL_URL = "https://downloads.claude.ai/claude-code-releases/{channel}"
PIN = re.compile(r'^CLAUDE_CODE = "([^"]+)"$', re.M)
VERSION = re.compile(r"^\d+\.\d+\.\d+$")


def current(init=INIT):
    return PIN.search(init.read_text()).group(1)


def channel(name="stable"):
    with urlopen(CHANNEL_URL.format(channel=name), timeout=30) as response:
        version = response.read().decode().strip()
    if not VERSION.match(version):
        raise ValueError(f"Unexpected version from the {name} channel: {version!r}")
    return version


def newer(candidate, pinned):
    return tuple(map(int, candidate.split("."))) > tuple(map(int, pinned.split(".")))


def update(init=INIT, fetch=channel):
    """Pin the stable version when it is newer. Return it, or None when nothing changed."""
    stable = fetch("stable")
    if not newer(stable, current(init)):
        return None
    set_version(stable, init)
    return stable


def set_version(version, init=INIT):
    if not VERSION.match(version):
        raise ValueError(f"Not a version: {version!r}")
    text = init.read_text()
    init.write_text(PIN.sub(f'CLAUDE_CODE = "{version}"', text, count=1))


def main(argv):
    command = argv[1] if len(argv) > 1 else "current"
    if command == "current":
        print(current())
    elif command == "stable":
        print(channel("stable"))
    elif command == "set" and len(argv) == 3:
        set_version(argv[2])
    elif command == "update":
        version = update()
        if version:
            print(version)
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
