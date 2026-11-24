"""NexKit's engine: the loop's decisions as pure calls behind the protocol, as
docs/engine.md describes. A host sends a JSON request and carries out the answer.

The engine reads no network and no clock. The only files it reads are the protocol's schemas,
which ship inside the `nexkit` package (`nexkit/protocol/v1/`); `machine.py`, which decides,
reads none at all. `cli.py` reads only standard input, and `serve.py` only the requests that
come to it over HTTP.
"""

from __future__ import annotations

import json
import re

from .. import __version__, protocol
from .machine import EngineError, step

CALLS = ("describe", "step")
# How deep a request may nest. The protocol's documents nest less than ten deep; the bound
# refuses deeper text before anything recurses into it, so a request is refused alike however
# many frames the program or the server already uses, and far below any stack's limit.
MAX_DEPTH = 64
# What changes the depth: a quote, a bracket, and an escaped character, which is never either.
_TOKENS = re.compile(rb'\\.|["\[\]{}]', re.DOTALL)


def answer(request):
    """The answer to one request, or an `error` answer (`calls/error.schema.json`) for one the
    engine refuses or fails on, so that a host always gets JSON back."""
    try:
        return _answer(request)
    except EngineError as error:
        return error_answer(str(error))
    except Exception as error:  # A bug of the engine's still answers, as an error.
        return error_answer(f"The engine failed on this request: {type(error).__name__}: {error}")


def error_answer(text):
    return {"protocol": protocol.VERSION, "error": text}


def reply(call, data):
    """The answer to a request for `call` given as UTF-8 JSON bytes, as the program reads them
    on standard input and `serve` in a body. A request may leave out its call, or name `call`."""
    if _depth(data) > MAX_DEPTH:
        return error_answer(f"The request is not JSON: it nests deeper than {MAX_DEPTH}.")
    try:
        request = json.loads(data.decode("utf-8"))
    except ValueError as error:
        return error_answer(f"The request is not JSON: {error}")
    if isinstance(request, dict) and request.get("call", call) != call:
        return error_answer(f"The request is a {request['call']} call.")
    return answer({**request, "call": call} if isinstance(request, dict) else request)


def _depth(data):
    """How deep the arrays and objects of JSON text nest, counting brackets outside strings,
    in one pass. Text that is not JSON gets some number; parsing it then says what is wrong."""
    depth = deepest = 0
    inside = False
    for token in _TOKENS.findall(data):
        if token == b'"':
            inside = not inside
        elif inside or token[:1] == b"\\":
            continue
        elif token in b"[{":
            depth += 1
            deepest = max(deepest, depth)
        else:
            depth -= 1
    return deepest


def text(answer):
    """An answer as the program writes it and `serve` sends it: sorted JSON on one line."""
    return json.dumps(answer, sort_keys=True) + "\n"


def _answer(request):
    if not isinstance(request, dict) or request.get("call") not in CALLS:
        raise EngineError(f"A request's call must be one of {', '.join(CALLS)}.")
    if request["call"] == "describe":
        return _describe(request)
    event = request.get("event")
    kind = event.get("kind") if isinstance(event, dict) else None
    if kind not in protocol.names("events"):
        found = [f"request.event.kind {kind!r} is not an event"]
    else:
        found = protocol.check(event, f"events/{kind}.schema.json", "request.event")
    if not found:
        found = protocol.check(request, f"calls/{request['call']}.schema.json", "request")
    if not found and kind == "actionFinished" and request["state"]:
        found = _result_problems(request["state"], event)
    if found:
        raise EngineError("; ".join(found))
    state, actions, changed = step(request["state"], event, request["policies"], protocol.check)
    return {"protocol": protocol.VERSION, "state": state, "actions": actions, "changed": changed}


def _describe(request):
    """Which protocol the engine speaks, so a host can refuse one below the minor it needs
    (docs/engine.md#versions)."""
    found = protocol.check(request, "calls/describe.schema.json", "request")
    if found:
        raise EngineError("; ".join(found))
    if request["protocol"] != protocol.VERSION:
        # The host speaks a major version this engine does not serve.
        raise EngineError(
            f"This engine serves protocol {protocol.VERSION}, not {request['protocol']}."
        )
    return {
        "protocol": {"major": protocol.VERSION, "minor": protocol.MINOR},
        "engine": __version__,
        "calls": list(CALLS),
    }


def _result_problems(state, event):
    pending = next((item for item in state["pending"] if item["id"] == event["action"]), None)
    if pending is None:
        return []
    return protocol.check(
        event["result"], f"actions/{pending['kind']}.result.schema.json", "request.event.result"
    )
