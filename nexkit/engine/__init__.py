"""NexKit's engine: the loop's decisions as pure calls behind the protocol, as
docs/engine.md describes. A host sends a JSON request and carries out the answer.

The engine reads no network and no clock. The only files it reads are the protocol's schemas,
which ship inside the `nexkit` package (`nexkit/protocol/v1/`); `machine.py`, which decides,
reads none at all. `cli.py` reads only standard input, and `serve.py` only the requests that
come to it over HTTP.
"""

from __future__ import annotations

import json

from .. import protocol
from . import prompt
from .machine import EngineError, step

CALLS = ("step", "prompt")


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
    try:
        request = json.loads(data.decode("utf-8"))
    except ValueError as error:
        return error_answer(f"The request is not JSON: {error}")
    except RecursionError:
        # Its message names the stack it used, which differs between processes and threads.
        return error_answer("The request is not JSON: it nests too deep.")
    if isinstance(request, dict) and request.get("call", call) != call:
        return error_answer(f"The request is a {request['call']} call.")
    return answer({**request, "call": call} if isinstance(request, dict) else request)


def text(answer):
    """An answer as the program writes it and `serve` sends it: sorted JSON on one line."""
    return json.dumps(answer, sort_keys=True) + "\n"


def _answer(request):
    if not isinstance(request, dict) or request.get("call") not in CALLS:
        raise EngineError(f"A request's call must be one of {', '.join(CALLS)}.")
    if request["call"] == "prompt":
        return _prompt(request, protocol.check(request, "calls/prompt.schema.json", "request"))
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


def _result_problems(state, event):
    pending = next((item for item in state["pending"] if item["id"] == event["action"]), None)
    if pending is None:
        return []
    return protocol.check(
        event["result"], f"actions/{pending['kind']}.result.schema.json", "request.event.result"
    )


def _prompt(request, found):
    """The answer to a `prompt` call: the stage's prompt, from the texts the host fetched."""
    if not found and request.get("unfit") is None:
        found = [
            f"request.{name} is missing"
            for name in prompt.missing(request["stage"], request["inputs"], request["policies"])
        ]
        texts = request.get("texts")
        if texts is not None and not texts.startswith("/"):
            found.append("request.texts must be an absolute path")
    if found:
        raise EngineError("; ".join(found))
    return {"protocol": protocol.VERSION, **prompt.answer(request)}
