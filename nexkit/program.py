"""The GitHub host's client of the `nexkit-engine` program (docs/architecture.md#the-engine).

`call` runs `python -P -m nexkit.engine <call>` with the host's own Python and the same
`nexkit` package, sends one JSON request on standard input and reads one JSON answer, as D12
says a host does. The program gets no token or secret: its environment holds only what Python
needs to find the package and a locale. A program that is missing, crashes or runs past
`TIMEOUT` gives an `error` answer, as the engine itself does for a bug of its own, so a host
treats it as a refusal. `-P` keeps the working folder, which may hold a checkout of the
repository, off the import path, so none of its files shadows a module of NexKit.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from .protocol import VERSION
from .textutil import clip

COMMAND = (sys.executable, "-P", "-m", "nexkit.engine")
# Seconds. The engine answers even a request of 1 MiB in under five, so only a hung program
# runs this long.
TIMEOUT = 60
# The folder that holds the `nexkit` package this module is in.
HOME = str(Path(__file__).resolve().parent.parent)
ENVIRONMENT = ("PATH", "LANG")
# How much of the program's last line of standard error an error answer holds. A crash may
# quote the request, which is untrusted text.
LEAST = 300


def call(name, request):
    """The program's answer to `request` for the call `name`, or an `error` answer."""
    env = {"PYTHONPATH": HOME, **{k: os.environ[k] for k in ENVIRONMENT if k in os.environ}}
    try:
        done = subprocess.run(
            [*COMMAND, name],
            input=json.dumps(request).encode(),
            capture_output=True,
            env=env,
            timeout=TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return failed(f"it ran past {TIMEOUT} seconds")
    except OSError as error:
        return failed(f"{type(error).__name__}: {error}")
    try:
        found = json.loads(done.stdout.decode("utf-8"))
    except ValueError:
        found = None
    if not isinstance(found, dict) or "protocol" not in found:
        lines = done.stderr.decode("utf-8", "replace").strip().splitlines()
        return failed(f"exit {done.returncode}: {clip(lines[-1] if lines else '', LEAST)}")
    return found


def failed(why):
    return {"protocol": VERSION, "error": f"The engine program failed: {why}"}
