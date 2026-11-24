"""`nexkit-engine serve`: the same requests and answers as standard input, over HTTP, for a
host that runs the engine beside it rather than once for each call (docs/engine.md#the-parts).

`POST /<call>` takes the request that `nexkit-engine <call>` reads on standard input as its
body, and answers with the same JSON text: `200` when the engine answered, `400` when it
refused the request. Each request is answered on its own, so the server holds no state.
"""

from __future__ import annotations

import argparse
import signal
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .. import protocol
from . import CALLS, reply
from .cli import text

HOST = "127.0.0.1"
PORT = 8080
# A step request carries one state document, which the protocol keeps small enough for one
# GitHub comment, with one event and the policies: far below MAX_BODY. A prompt request
# carries the texts a stage reads, each kept whole since a prompt names the file that holds
# it; its diffs and outputs come clipped to what the prompt shows (`calls/prompt.schema.json`).
MAX_BODY = 1 << 20
MAX_BODIES = {"prompt": 16 << 20}


class Handler(BaseHTTPRequestHandler):
    # A client that stops sending does not hold its thread for longer than this.
    timeout = 30

    def do_POST(self):
        call = self.path.removeprefix("/")
        if call not in CALLS:
            return self.respond(
                404, refusal(f"No call is at {self.path}; the calls are {calls()}.")
            )
        length = self.headers.get("Content-Length", "")
        if self.headers.get("Transfer-Encoding") or not (length.isascii() and length.isdigit()):
            return self.respond(411, refusal("Send the request with a Content-Length."))
        most = MAX_BODIES.get(call, MAX_BODY)
        if int(length) > most:
            return self.respond(413, refusal(f"A {call} request has at most {most} bytes."))
        answer = reply(call, self.rfile.read(int(length)))
        self.respond(400 if "error" in answer else 200, answer)

    def do_GET(self):
        self.respond(405, refusal(f"Send each request with POST to one of {calls()}."))

    def respond(self, status, answer):
        body = text(answer).encode()
        self.send_response(status)
        if status == 405:
            self.send_header("Allow", "POST")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # Each connection carries one request, so nothing about one request outlives it.
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True


def refusal(error):
    return {"protocol": protocol.VERSION, "error": error}


def calls():
    return ", ".join(f"/{call}" for call in CALLS)


def parser():
    parser = argparse.ArgumentParser(
        prog="nexkit-engine serve", description="Answer the engine's calls over HTTP."
    )
    parser.add_argument("--host", default=HOST, help=f"the address to listen on ({HOST})")
    parser.add_argument("--port", type=int, default=PORT, help=f"the port, 0 for any ({PORT})")
    return parser


def main(argv):
    args = parser().parse_args(argv)
    # In a container the server is the first process, which ignores a SIGTERM it does not
    # handle: stop at once instead of waiting to be killed.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    with ThreadingHTTPServer((args.host, args.port), Handler) as server:
        host, port = server.server_address[:2]
        print(f"nexkit-engine serves http://{host}:{port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0
