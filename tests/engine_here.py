"""The stand-in for `nexkit.program.call` that the tests install (`tests/__init__.py`).

Starting a Python process for each `step` call made the unit suite almost four times slower, so
the tests answer in this process. It runs what the program runs: `engine.reply` on the request's
JSON bytes, and `engine.text` for the answer, which it reads back, so its answers are the
program's byte for byte. Only `ProgramTransportTest` in `tests/test_host.py` and the tests of
`nexkit/program.py` reach the real program, which is `REAL`.
"""

import json

from nexkit import engine, program

REAL = program.call


def call(name, request):
    return json.loads(engine.text(engine.reply(name, json.dumps(request).encode())))


def install():
    program.call = call
