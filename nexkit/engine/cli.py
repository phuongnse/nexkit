"""`nexkit-engine`: answer one JSON request read on standard input, or serve the same
requests over HTTP (`serve.py`)."""

from __future__ import annotations

import sys

from . import CALLS, reply, serve, text

USAGE = (
    f"usage: nexkit-engine {{{','.join(CALLS)}}} < request.json\n"
    "       nexkit-engine serve [--host HOST] [--port PORT]"
)


def main(argv=None, stdin=None, stdout=None):
    """`stdin` is binary, as `serve` reads a body: both decode the request as UTF-8."""
    argv = sys.argv[1:] if argv is None else argv
    stdin, stdout = stdin or sys.stdin.buffer, stdout or sys.stdout
    if argv[:1] == ["serve"]:
        return serve.main(argv[1:])
    if len(argv) != 1 or argv[0] not in CALLS:
        print(USAGE, file=sys.stderr)
        return 2
    answer = reply(argv[0], stdin.read())
    stdout.write(text(answer))
    return 1 if "error" in answer else 0
