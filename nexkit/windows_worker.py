"""Trusted process bootstrap, started with isolated Python outside the project."""

import json
import subprocess
import sys
import time
from pathlib import Path


def main():
    request, gate = map(Path, sys.argv[1:])
    deadline = time.monotonic() + 30
    while not gate.exists():
        if time.monotonic() >= deadline:
            return 125
        time.sleep(0.01)
    value = json.loads(request.read_text(encoding="utf-8"))
    try:
        # The trusted controller already supplied the exact environment when
        # starting this worker. Never serialize provider credentials into the
        # temporary request file just to pass them to the next trusted child.
        return subprocess.call(value["argv"], cwd=value["cwd"])
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
