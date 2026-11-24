"""NexKit's engine: the loop's decisions as pure calls behind the protocol, as
docs/engine.md describes. A host sends a JSON request and carries out the answer."""

from __future__ import annotations

import json

from .. import protocol
from .machine import EngineError, step

CALLS = ("step",)


def answer(request):
    """The answer to one request, or an `error` answer for a request the engine refuses."""
    try:
        return _answer(request)
    except EngineError as error:
        return {"protocol": protocol.VERSION, "error": str(error)}


def reply(call, data):
    """The answer to a request for `call` given as UTF-8 JSON bytes, as the program reads them
    on standard input and `serve` in a body. A request may leave out its call, or name `call`."""
    try:
        request = json.loads(data.decode("utf-8"))
    except ValueError as error:
        return {"protocol": protocol.VERSION, "error": f"The request is not JSON: {error}"}
    except RecursionError:
        # Its message names the stack it used, which differs between processes and threads.
        return {
            "protocol": protocol.VERSION,
            "error": "The request is not JSON: it nests too deep.",
        }
    if not isinstance(request, dict):
        return answer(request)
    if request.get("call", call) != call:
        return {"protocol": protocol.VERSION, "error": f"The request is a {request['call']} call."}
    return answer({**request, "call": call})


def text(answer):
    """An answer as the program writes it and `serve` sends it: sorted JSON on one line."""
    return json.dumps(answer, sort_keys=True) + "\n"


def _answer(request):
    if not isinstance(request, dict) or request.get("call") not in CALLS:
        raise EngineError(f"A request's call must be one of {', '.join(CALLS)}.")
    found = protocol.check(request, f"calls/{request['call']}.schema.json", "request")
    event = request.get("event")
    kind = event.get("kind") if isinstance(event, dict) else None
    if kind not in protocol.names("events"):
        found.append(f"request.event.kind {kind!r} is not an event")
    else:
        found += protocol.check(event, f"events/{kind}.schema.json", "request.event")
        if kind == "actionFinished" and request.get("state"):
            found += _result_problems(request["state"], event)
    if found:
        raise EngineError("; ".join(found))
    state, actions, changed = step(request["state"], event, request["policies"])
    return {"protocol": protocol.VERSION, "state": state, "actions": actions, "changed": changed}


def _result_problems(state, event):
    pending = next((item for item in state["pending"] if item["id"] == event["action"]), None)
    if pending is None:
        return []
    return protocol.check(
        event["result"], f"actions/{pending['kind']}.result.schema.json", "request.event.result"
    )
