"""`nexkit-engine serve`: the same requests and answers as standard input, over HTTP, and no
state between requests (docs/engine.md#the-parts)."""

import contextlib
import http.client
import io
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

import nexkit
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


def on_standard_input(call, text):
    """The exit code and output of `nexkit-engine <call>` given `text` on standard input."""
    out = io.StringIO()
    code = cli.main([call], io.StringIO(text), out)
    return code, out.getvalue().encode()


class ServeTest(unittest.TestCase):
    def test_every_conformance_request_gets_the_answer_standard_input_gives(self):
        texts = [json.dumps(request) for request in every_request()]
        # And the requests the engine refuses: not JSON, another call, a broken protocol.
        texts += ["{", '{"call": "sweep"}', "[]", json.dumps({"protocol": 1, "policies": 2})]
        self.assertGreater(len(texts), 100)
        with server() as port:
            for text in texts:
                code, expected = on_standard_input("step", text)
                status, body = send(port, "/step", text.encode())
                self.assertEqual(body, expected, text[:200])
                self.assertEqual(status, 400 if code else 200, text[:200])

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

    def test_what_is_not_a_request_is_refused(self):
        with server() as port:
            status, body = send(port, "/dance", b"{}")
            self.assertEqual(status, 404)
            self.assertIn("the calls are /step", json.loads(body)["error"])
            self.assertEqual(send(port, "/step", method="GET")[0], 405)
            too_long = {"Content-Length": str(serve.MAX_BODY + 1)}
            status, body = send(port, "/step", b"{}", headers=too_long)
            self.assertEqual(status, 413)
            self.assertEqual(json.loads(body)["protocol"], 1)
            status, _ = send(port, "/step", b"{}", headers={"Transfer-Encoding": "chunked"})
            self.assertEqual(status, 411)

    def test_it_listens_only_on_this_machine_unless_told(self):
        args = serve.parser().parse_args([])
        self.assertEqual((args.host, args.port), ("127.0.0.1", serve.PORT))
        args = serve.parser().parse_args(["--host", "0.0.0.0", "--port", "9000"])
        self.assertEqual((args.host, args.port), ("0.0.0.0", 9000))


class ImageTest(unittest.TestCase):
    """The image a release publishes: `serve` on the package at the release's version."""

    def test_the_image_starts_serve_on_the_pinned_python(self):
        text = (ROOT / "Dockerfile").read_text()
        entry = json.loads(re.search(r"^ENTRYPOINT (.+)$", text, re.M).group(1))
        self.assertEqual(entry, ["nexkit-engine", "serve", "--host", "0.0.0.0", "--port", "8080"])
        self.assertEqual(int(entry[-1]), serve.PORT)
        python = ".".join(str(part) for part in nexkit.PYTHON)
        self.assertIn(f"ARG PYTHON={python}\n", text)
        self.assertIn("RUN pip install --no-cache-dir --root-user-action=ignore /src", text)

    def test_a_published_release_publishes_the_image_at_its_version(self):
        text = (ROOT / ".github" / "workflows" / "release.yml").read_text()
        self.assertIn("on:\n  release:\n    types: [published]\n", text)
        self.assertIn("permissions: {}\n", text)
        self.assertIn("IMAGE: ghcr.io/phuongnse/nexkit-engine\n", text)
        build, publish = text.split("\n  build:\n", 1)[1].split("\n  publish:\n", 1)
        # The tag must be the package's version, and the image is built and run from it.
        self.assertIn('if [ "$TAG" != "v$version" ]; then', build)
        self.assertIn('--tag "$IMAGE:$VERSION" .', build)
        self.assertIn("cmp served.json expected.json", build)
        self.assertIn("    permissions:\n      contents: read\n    outputs:", build)
        # The job that may write packages runs no repository code: it never checks it out.
        self.assertIn("    permissions:\n      packages: write\n    steps:", publish)
        self.assertNotIn("actions/checkout", publish)
        self.assertIn('docker push "$IMAGE:$VERSION"', publish)


if __name__ == "__main__":
    unittest.main()
