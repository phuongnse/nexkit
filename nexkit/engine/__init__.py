"""NexKit's engine: the loop's decisions as pure calls behind the protocol, as
docs/engine.md describes. A host sends a JSON request and carries out the answer.

The engine reads no network and no clock. The only files it reads are the protocol's schemas,
which ship inside the `nexkit` package (`nexkit/protocol/v1/`); `machine.py`, which decides,
reads none at all, and `cli.py` reads only standard input.
"""

from __future__ import annotations

from .. import __version__, protocol
from .machine import EngineError, step

CALLS = ("describe", "step")


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
