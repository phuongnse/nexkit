"""`nexkit-engine serve`: the same requests and answers as standard input, over HTTP, and no
state between requests (docs/engine.md#the-parts)."""

import contextlib
import http.client
import io
import json
import re
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

import nexkit
from nexkit import engine, protocol
from nexkit.engine import cli, serve
from tests.test_conformance import every_request

ROOT = Path(__file__).resolve().parent.parent


@contextlib.contextmanager
def server():
    """A `nexkit-engine serve` process on a free port of this machine; yields its port."""
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "bin" / "nexkit-engine"), "serve", "--port", "0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        line = process.stdout.readline()
        if not line.startswith("nexkit-engine serves http://127.0.0.1:"):
            raise AssertionError(f"the server did not start: {line!r}")
        yield int(line.rsplit(":", 1)[1])
    finally:
        process.terminate()
        process.wait(timeout=10)
        process.stdout.close()


def send(port, path, body=b"", headers=None, method="POST"):
    """The status and body of one HTTP request."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def on_standard_input(call, data):
    """The exit code and output of `nexkit-engine <call>` given `data` on standard input."""
    out = io.StringIO()
    code = cli.main([call], io.BytesIO(data), out)
    return code, out.getvalue().encode()


@contextlib.contextmanager
def server_in_this_process():
    """`serve`'s server on a thread of this process, so a test can change what it calls."""
    with serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler) as httpd:
        thread = threading.Thread(target=httpd.serve_forever)
        thread.start()
        try:
            yield httpd.server_address[1]
        finally:
            httpd.shutdown()
            thread.join()


# Bytes the program must read as standard input does: a byte order mark, bytes that are not
# UTF-8, another encoding, text that nests deeper than a parser goes, and a second request.
RAW = [
    b"\xef\xbb\xbf{}",
    b'{"protocol": 1, "note": "\xff\xfe"}',
    json.dumps({"protocol": 1}).encode("utf-16"),
    b"[" * 100_000 + b"]" * 100_000,
    b"{} {}",
]


class ServeTest(unittest.TestCase):
    def test_every_conformance_request_gets_the_answer_standard_input_gives(self):
        texts = [json.dumps(request).encode() for request in every_request()]
        # And the requests the engine refuses: not JSON, another call, a broken protocol.
        texts += [b"{", b'{"call": "sweep"}', b"[]", b'{"protocol": 1, "event": []}', *RAW]
        self.assertGreater(len(texts), 100)
        with server() as port:
            for text in texts:
                code, expected = on_standard_input("step", text)
                status, body = send(port, "/step", text)
                self.assertEqual(body, expected, text[:200])
                self.assertEqual(status, 400 if code else 200, text[:200])

    def test_the_program_and_the_server_read_the_same_bytes_alike(self):
        program = [sys.executable, str(ROOT / "bin" / "nexkit-engine"), "step"]
        first = json.dumps(next(every_request())).encode()
        with server() as port:
            for data in [first, *RAW]:
                done = subprocess.run(program, input=data, capture_output=True, check=False)
                status, body = send(port, "/step", data)
                self.assertEqual(body, done.stdout, data[:80])
                self.assertEqual(status, 400 if done.returncode else 200, data[:80])
                self.assertEqual(done.returncode, 1 if data is not first else 0, data[:80])

    def test_every_call_is_served_as_on_standard_input(self):
        # `describe` passes the protocol the host speaks; an unserved major is an error answer.
        texts = [b'{"protocol": 1}', b'{"protocol": 2}', b'{"call": "describe", "protocol": 1}']
        with server() as port:
            for call in engine.CALLS:
                for text in texts:
                    code, expected = on_standard_input(call, text)
                    status, body = send(port, f"/{call}", text)
                    self.assertEqual(body, expected, (call, text))
                    self.assertEqual(status, 400 if code else 200, (call, text))
            status, body = send(port, "/describe", b'{"protocol": 1}')
            self.assertEqual(status, 200)
            answer = json.loads(body)
            self.assertEqual(protocol.check(answer, "calls/describe.answer.schema.json"), [])
            self.assertEqual(answer["calls"], list(engine.CALLS))
            self.assertRefused(send(port, "/describe", b'{"protocol": 2}'), 400, "not 2")

    def test_one_request_to_two_processes_gets_one_answer(self):
        texts = [json.dumps(request) for request in every_request()]
        middle = len(texts) // 2
        with server() as used, server() as fresh:
            # One process has answered every request before this one, the other none.
            for text in texts[:middle]:
                send(used, "/step", text.encode())
            first = send(used, "/step", texts[middle].encode())
            second = send(fresh, "/step", texts[middle].encode())
            again = send(used, "/step", texts[middle].encode())
        self.assertEqual(first[0], 200)
        self.assertEqual(first, second)
        self.assertEqual(first, again)

    def assertRefused(self, answer, status, words=""):
        """`answer` is an error answer of the protocol with `status`, and says `words`."""
        self.assertEqual(answer[0], status)
        body = json.loads(answer[1])
        self.assertEqual(protocol.check(body, "calls/error.schema.json"), [], body)
        self.assertIn(words, body["error"])

    def test_what_is_not_a_request_is_refused_with_an_error_answer(self):
        with server() as port:
            self.assertRefused(send(port, "/dance", b"{}"), 404, "the calls are /describe, /step.")
            for method in ("GET", "PUT", "DELETE", "PATCH", "OPTIONS"):
                self.assertRefused(send(port, "/step", method=method), 405, "with POST")
            self.assertEqual(send(port, "/step", method="HEAD"), (405, b""))
            too_long = {"Content-Length": str(serve.MAX_BODY + 1)}
            self.assertRefused(send(port, "/step", b"{}", headers=too_long), 413, "at most")
            # A length too long to be a number Python reads is refused all the same.
            digits = {"Content-Length": "9" * 5000}
            self.assertRefused(send(port, "/step", b"{}", headers=digits), 413, "at most")
            chunked = {"Transfer-Encoding": "chunked"}
            self.assertRefused(send(port, "/step", b"{}", headers=chunked), 411, "Content-Length")
            self.assertRefused(send(port, "/step", b"{"), 400, "not JSON")

    def test_a_failure_of_the_engine_is_answered(self):
        with (
            mock.patch.object(serve, "reply", side_effect=RuntimeError("boom")),
            mock.patch.object(serve.Handler, "log_message") as log,
            server_in_this_process() as port,
        ):
            answer = send(port, "/step", b"{}")
        self.assertRefused(answer, 500, "The engine failed on this request.")
        logged = "".join(str(call.args[1:]) for call in log.call_args_list)
        self.assertIn("RuntimeError: boom", logged)

    def test_it_listens_only_on_this_machine_unless_told(self):
        args = serve.parser().parse_args([])
        self.assertEqual((args.host, args.port), ("127.0.0.1", serve.PORT))
        args = serve.parser().parse_args(["--host", "0.0.0.0", "--port", "9000"])
        self.assertEqual((args.host, args.port), ("0.0.0.0", 9000))


class ImageTest(unittest.TestCase):
    """The image a release publishes: `serve` from the package's source on a pinned Python.
    tests/test_cli.py checks the workflow that builds and publishes it."""

    def test_the_image_starts_serve_from_the_source_on_the_pinned_python(self):
        text = (ROOT / "Dockerfile").read_text()
        entry = json.loads(re.search(r"^ENTRYPOINT (.+)$", text, re.M).group(1))
        serve_args = ["serve", "--host", "0.0.0.0", "--port", str(serve.PORT)]
        self.assertEqual(entry, ["python", "/opt/nexkit/bin/nexkit-engine", *serve_args])
        self.assertIn("COPY bin/nexkit-engine /opt/nexkit/bin/\n", text)
        self.assertIn("COPY nexkit /opt/nexkit/nexkit\n", text)
        # The base comes only from the pin, and the build fetches and installs nothing.
        self.assertIn("ARG PYTHON_IMAGE\nFROM ${PYTHON_IMAGE}\n", text)
        self.assertNotRegex(text, r"(?m)^RUN ")
        self.assertIn("USER 65534:65534\n", text)

    def test_the_base_image_is_the_pinned_python_by_digest(self):
        python = ".".join(str(part) for part in nexkit.PYTHON)
        self.assertRegex(nexkit.PYTHON_IMAGE, rf"^python:{python}-slim@sha256:[0-9a-f]{{64}}$")


if __name__ == "__main__":
    unittest.main()
