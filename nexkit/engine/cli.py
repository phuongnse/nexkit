"""`nexkit-engine`: answer one JSON request read on standard input."""

from __future__ import annotations

import json
import sys

from . import CALLS, answer, error_answer

USAGE = f"usage: nexkit-engine {{{','.join(CALLS)}}} < request.json"


def main(argv=None, stdin=None, stdout=None):
    argv = sys.argv[1:] if argv is None else argv
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    if len(argv) != 1 or argv[0] not in CALLS:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        request = json.load(stdin)
    except json.JSONDecodeError as error:
        reply = error_answer(f"The request is not JSON: {error}")
    else:
        if isinstance(request, dict) and request.get("call", argv[0]) != argv[0]:
            reply = error_answer(f"The request is a {request.get('call')} call.")
        else:
            reply = answer({**request, "call": argv[0]} if isinstance(request, dict) else request)
    json.dump(reply, stdout, sort_keys=True)
    stdout.write("\n")
    return 1 if "error" in reply else 0
