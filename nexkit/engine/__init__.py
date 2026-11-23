"""NexKit's engine: the loop's decisions as pure calls behind the protocol, as
docs/engine.md describes. A host sends a JSON request and carries out the answer."""

from __future__ import annotations

from .. import protocol
from .machine import EngineError, step

CALLS = ("step",)


def answer(request):
    """The answer to one request, or an `error` answer for a request the engine refuses."""
    try:
        return _answer(request)
    except EngineError as error:
        return {"protocol": protocol.VERSION, "error": str(error)}


def _answer(request):
    if not isinstance(request, dict) or request.get("call") not in CALLS:
        raise EngineError(f"A request's call must be one of {', '.join(CALLS)}.")
    found = protocol.check(request, f"calls/{request['call']}.schema.json", "request")
    event = request.get("event") or {}
    kind = event.get("kind")
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
