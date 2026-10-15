#!/usr/bin/env python3
"""Show or update the one version of each agent runtime that NexKit supports.

    agent_version.py RUNTIME current        print the pinned version
    agent_version.py RUNTIME stable         print the newest stable version
    agent_version.py RUNTIME set VERSION    pin VERSION
    agent_version.py RUNTIME update         pin the stable version if it is newer; print it

RUNTIME is a name from nexkit/runtimes/__init__.py, such as claude-code or codex. The pins
live only in nexkit/__init__.py. The pipeline and CI read them from there.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nexkit.runtimes import RUNTIMES  # noqa: E402

INIT = ROOT / "nexkit" / "__init__.py"
VERSION = re.compile(r"^\d+\.\d+\.\d+$")


def _pin(runtime):
    return re.compile(rf'^{RUNTIMES[runtime].pin} = "([^"]+)"$', re.M)


def current(runtime, init=INIT):
    return _pin(runtime).search(init.read_text()).group(1)


def read(url):
    with urlopen(url, timeout=30) as response:
        return response.read().decode()


def stable(runtime, fetch=read):
    found = RUNTIMES[runtime]
    version = found.release_version(fetch(found.release_url))
    if not VERSION.match(version):
        raise ValueError(f"Unexpected stable version of {runtime}: {version!r}")
    return version


def newer(candidate, pinned):
    return tuple(map(int, candidate.split("."))) > tuple(map(int, pinned.split(".")))


def update(runtime, init=INIT, fetch=read):
    """Pin the stable version when it is newer. Return it, or None when nothing changed."""
    version = stable(runtime, fetch)
    if not newer(version, current(runtime, init)):
        return None
    set_version(runtime, version, init)
    return version


def set_version(runtime, version, init=INIT):
    if not VERSION.match(version):
        raise ValueError(f"Not a version: {version!r}")
    text = init.read_text()
    init.write_text(_pin(runtime).sub(f'{RUNTIMES[runtime].pin} = "{version}"', text, count=1))


def main(argv):
    runtime = argv[1] if len(argv) > 1 else ""
    command = argv[2] if len(argv) > 2 else "current"
    if runtime not in RUNTIMES:
        print(__doc__, file=sys.stderr)
        return 2
    if command == "current":
        print(current(runtime))
    elif command == "stable":
        print(stable(runtime))
    elif command == "set" and len(argv) == 4:
        set_version(runtime, argv[3])
    elif command == "update":
        version = update(runtime)
        if version:
            print(version)
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
