"""`nexkit-engine`: answer one JSON request read on standard input, or serve the same
requests over HTTP (`serve.py`)."""

from __future__ import annotations

import json
import sys

from . import CALLS, reply

USAGE = (
    f"usage: nexkit-engine {{{','.join(CALLS)}}} < request.json\n"
    "       nexkit-engine serve [--host HOST] [--port PORT]"
)


def main(argv=None, stdin=None, stdout=None):
    argv = sys.argv[1:] if argv is None else argv
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    if argv[:1] == ["serve"]:
        from . import serve

        return serve.main(argv[1:])
    if len(argv) != 1 or argv[0] not in CALLS:
        print(USAGE, file=sys.stderr)
        return 2
    answer = reply(argv[0], stdin.read())
    stdout.write(text(answer))
    return 1 if "error" in answer else 0


def text(answer):
    """An answer as the program writes it and `serve` sends it: sorted JSON on one line."""
    return json.dumps(answer, sort_keys=True) + "\n"
