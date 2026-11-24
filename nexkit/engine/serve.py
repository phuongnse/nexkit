"""`nexkit-engine serve`: the same requests and answers as standard input, over HTTP, for a
host that runs the engine beside it rather than once for each call (docs/engine.md#the-parts).

`POST /<call>` takes the request that `nexkit-engine <call>` reads on standard input as its
body, and answers with the same JSON text: `200` when the engine answered, `400` when it
refused the request. Each request is answered on its own, so the server holds no state.
"""

from __future__ import annotations

import argparse
import signal
import socket
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import CALLS, error_answer, reply, text

HOST = "127.0.0.1"
PORT = 8080
# A step request carries one state document, which the protocol keeps small enough for one
# GitHub comment, with one event and the policies: far below MAX_BODY. A prompt request
# carries the texts a stage reads, each kept whole since a prompt names the file that holds
# it; its diffs and outputs come clipped to what the prompt shows (`calls/prompt.schema.json`).
MAX_BODY = 1 << 20
MAX_BODIES = {"prompt": 16 << 20}


class Server(ThreadingHTTPServer):
    # Connections the system holds while the server takes the ones before them: the standard
    # library's 5 resets a burst of a host's concurrent calls.
    request_queue_size = socket.SOMAXCONN


class Handler(BaseHTTPRequestHandler):
    # A client that stops sending does not hold its thread for longer than this, between two
    # reads. Nothing bounds a client that keeps sending slowly, or how many connect at once:
    # serve belongs on a network that only its host reaches (docs/engine.md#the-parts).
    timeout = 30
    # A request line that does not parse leaves no version; answer it with a status line and
    # headers all the same, as no client speaks HTTP/0.9.
    default_request_version = "HTTP/1.0"

    def do_POST(self):
        try:
            status, answer = self.answer()
        except TimeoutError:
            status, answer = 408, error_answer("The request's body did not all come in time.")
        except Exception:
            self.log_error("%s", traceback.format_exc())
            status, answer = 500, error_answer("The engine failed on this request.")
        self.respond(status, answer)

    def answer(self):
        call = self.path.removeprefix("/")
        if call not in CALLS:
            return 404, error_answer(f"No call is at {self.path}; the calls are {calls()}.")
        lengths = {value.strip() for value in self.headers.get_all("Content-Length", [])}
        if len(lengths) > 1:
            return 400, error_answer("The request has more than one Content-Length.")
        length = lengths.pop() if lengths else ""
        if self.headers.get("Transfer-Encoding") or not (length.isascii() and length.isdigit()):
            return 411, error_answer("Send the request with a Content-Length.")
        most = MAX_BODIES.get(call, MAX_BODY)
        # A length with more digits than any request needs is refused before it is a number.
        if len(length) > len(str(most)) or int(length) > most:
            return 413, error_answer(f"A {call} request has at most {most} bytes.")
        answer = reply(call, self.rfile.read(int(length)))
        return (400 if "error" in answer else 200), answer

    def refuse_method(self):
        self.respond(405, error_answer(f"Send each request with POST to one of {calls()}."))

    do_GET = do_HEAD = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = refuse_method

    def send_error(self, code, message=None, explain=None):
        """What the standard library refuses itself, such as a method it has no handler for,
        a malformed request line or too many headers, as an error answer too."""
        self.respond(code, error_answer(message or self.responses.get(code, ("",))[0]))

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
        if self.command != "HEAD":
            self.wfile.write(body)
        self.close_connection = True


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
    with Server((args.host, args.port), Handler) as server:
        host, port = server.server_address[:2]
        print(f"nexkit-engine serves http://{host}:{port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0
