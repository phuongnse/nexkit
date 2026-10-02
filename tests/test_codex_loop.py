"""Official native CLI and tools; only the local model response is simulated.

The SSE events follow the pinned CLI's own test protocol:
https://github.com/openai/codex/blob/rust-v0.156.1/codex-rs/core/tests/common/responses.rs
No account login, model inference or GitHub approval is supplied by this fixture.
"""

import gzip
import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from nexkit.common import read_regular_json, write_json
from tests.support import project


class ResponsesServer:
    """A loopback-only, two-response controller with a harmless API canary."""

    def __init__(self, command):
        self.command = command
        self.requests = []
        self.errors = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                try:
                    assert self.path == "/v1/responses", self.path
                    assert self.headers.get("Authorization") == "Bearer harmless-native-api-canary"
                    size = int(self.headers["Content-Length"])
                    assert 0 < size <= 8_000_000, size
                    body = self.rfile.read(size)
                    encoding = self.headers.get("Content-Encoding")
                    if encoding == "gzip":
                        body = gzip.decompress(body)
                    else:
                        assert not encoding, encoding
                    request = json.loads(body)
                    fixture.requests.append(request)
                    number = len(fixture.requests)
                    assert number <= 2, "Unexpected model request or retry"
                    if number == 1:

                        def tools(definitions):
                            for definition in definitions:
                                if definition.get("type") in {"function", "custom"}:
                                    yield definition
                                yield from tools(definition.get("tools", []))

                        definitions = list(request.get("tools") or [])
                        # Responses Lite places declarations in developer input.
                        # See the pinned core/src/client.rs request builder.
                        for fragment in request.get("input", []):
                            if fragment.get("type") == "additional_tools":
                                definitions.extend(fragment["tools"])
                        exposed = {tool["name"]: tool["type"] for tool in tools(definitions)}
                        arguments = {
                            "cmd": fixture.command,
                            "yield_time_ms": 30_000,
                            "max_output_tokens": 1000,
                            "login": False,
                        }
                        if exposed.get("exec") == "custom":
                            # GPT-6 Luna uses the official V8 tool bridge. It
                            # provides registered tools without Node or host IO.
                            item = {
                                "type": "custom_tool_call",
                                "call_id": "native-shell-check",
                                "name": "exec",
                                "input": (
                                    "if (typeof process !== 'undefined' || "
                                    "typeof require !== 'undefined' || typeof fetch !== 'undefined') "
                                    "throw new Error('Unexpected host IO in the tool bridge');\n"
                                    "let denied = false;\n"
                                    "try { await import('node:fs'); } catch { denied = true; }\n"
                                    "if (!denied) throw new Error('Host module import was allowed');\n"
                                    f"text(await tools.exec_command({json.dumps(arguments)}));"
                                ),
                            }
                        else:
                            assert exposed.get("exec_command") == "function", (
                                "The CLI did not expose a supported native execution tool"
                            )
                            item = {
                                "type": "function_call",
                                "call_id": "native-shell-check",
                                "name": "exec_command",
                                "arguments": json.dumps(arguments),
                            }
                    else:
                        item = {
                            "type": "message",
                            "id": "native-result",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": json.dumps({"summary": "Checked the native tool"}),
                                }
                            ],
                        }
                    identifier = f"native-response-{number}"
                    events = [
                        {"type": "response.created", "response": {"id": identifier}},
                        {"type": "response.output_item.done", "item": item},
                        {
                            "type": "response.completed",
                            "response": {
                                "id": identifier,
                                "usage": {
                                    "input_tokens": 0,
                                    "input_tokens_details": None,
                                    "output_tokens": 0,
                                    "output_tokens_details": None,
                                    "total_tokens": 0,
                                },
                            },
                        },
                    ]
                    response = "".join(
                        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(response)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(response)
                except BaseException as exc:
                    fixture.errors.append(repr(exc))
                    self.send_error(500, "Invalid native acceptance request")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def profile(self):
        return {
            "model_provider": "native_acceptance",
            "model_providers.native_acceptance": {
                "name": "Local native acceptance fixture",
                "base_url": self.url,
                "wire_api": "responses",
                "env_key": "CODEX_API_KEY",
                "requires_openai_auth": False,
                "supports_websockets": False,
                "request_max_retries": 0,
                "stream_max_retries": 0,
            },
        }


class ResponsesFixtureTests(unittest.TestCase):
    def test_fixture_rejects_a_tool_that_the_cli_did_not_expose(self):
        with ResponsesServer("literal test command") as server:
            request = Request(
                server.url + "/responses",
                data=b'{"model":"test-only","tools":[]}',
                headers={"Authorization": "Bearer harmless-native-api-canary"},
            )
            with self.assertRaises(HTTPError) as failure:
                urlopen(request, timeout=5)
            self.assertEqual(failure.exception.code, 500)
            self.assertIn("did not expose", server.errors[0])

    def test_fixture_uses_the_declared_responses_lite_tool_bridge(self):
        with ResponsesServer("literal test command") as server:
            request = Request(
                server.url + "/responses",
                data=json.dumps(
                    {
                        "model": "test-only",
                        "input": [
                            {
                                "type": "additional_tools",
                                "role": "developer",
                                "tools": [
                                    {
                                        "type": "namespace",
                                        "name": "functions",
                                        "tools": [{"type": "custom", "name": "exec"}],
                                    }
                                ],
                            }
                        ],
                    }
                ).encode("utf-8"),
                headers={"Authorization": "Bearer harmless-native-api-canary"},
            )
            with urlopen(request, timeout=5) as response:
                events = [
                    json.loads(line[6:])
                    for line in response.read().decode().splitlines()
                    if line.startswith("data: ")
                ]
            item = events[1]["item"]
            self.assertEqual(item["type"], "custom_tool_call")
            self.assertEqual(item["name"], "exec")
            self.assertIn("tools.exec_command", item["input"])
            self.assertIn('"cmd": "literal test command"', item["input"])
            self.assertEqual(server.errors, [])

    def test_fixture_returns_one_tool_then_one_structured_result(self):
        with ResponsesServer("literal test command") as server:
            for number in (1, 2):
                request = Request(
                    server.url + "/responses",
                    data=json.dumps(
                        {
                            "model": "test-only",
                            "tools": [{"type": "function", "name": "exec_command"}],
                        }
                    ).encode("utf-8"),
                    headers={"Authorization": "Bearer harmless-native-api-canary"},
                )
                with urlopen(request, timeout=5) as response:
                    events = [
                        json.loads(line[6:])
                        for line in response.read().decode().splitlines()
                        if line.startswith("data: ")
                    ]
                self.assertEqual(events[0]["type"], "response.created")
                self.assertEqual(events[-1]["type"], "response.completed")
                self.assertEqual(
                    events[1]["item"]["type"], "function_call" if number == 1 else "message"
                )
                if number == 1:
                    self.assertEqual(events[1]["item"]["name"], "exec_command")
                    self.assertEqual(
                        json.loads(events[1]["item"]["arguments"])["cmd"], "literal test command"
                    )
            self.assertEqual(server.errors, [])
            self.assertEqual(len(server.requests), 2)


@unittest.skipUnless(
    os.name == "posix" and os.environ.get("NEXKIT_TEST_CONTAINER_CLI") == "1",
    "Pinned CLI in the Docker runtime; only model responses are simulated",
)
class ContainerCodexLoopTests(unittest.TestCase):
    def test_real_tools_preserve_credentials_network_role_and_parent_git_boundaries(self):
        import pwd
        import shlex

        from nexkit.adapters.codex_subscription import cli_command, permission_settings

        self.assertTrue(Path("/.dockerenv").is_file(), "Use a disposable NexKit container")
        self.assertEqual(os.geteuid(), 0)
        user = pwd.getpwnam("nexkit-codex")
        cases = [(role, "/tmp/nexkit") for role in ("request", "task", "deliver", "review")]
        cases.append(("request", "/opt/actions-runner/_work/_temp/nexkit"))
        for role, controller_directory in cases:
            Path(controller_directory).mkdir(parents=True, exist_ok=True)
            with (
                self.subTest(role=role, controller=controller_directory),
                tempfile.TemporaryDirectory(prefix="nexkit-cli-loop-") as temporary,
                tempfile.TemporaryDirectory(dir=controller_directory) as private,
            ):
                root = Path(temporary)
                root.chmod(0o755)
                home, data, auth = root / "home", Path(private), root / "auth"
                work, output = home / "work", home / "output"
                for path in (home, data, auth, work, output):
                    path.mkdir(exist_ok=True)
                    path.chmod(0o755)
                write_json(
                    data / "schema.json",
                    {
                        "type": "object",
                        "properties": {"summary": {"type": "string"}},
                        "required": ["summary"],
                        "additionalProperties": False,
                    },
                )
                (data / "schema.json").chmod(0o644)
                (data / "prompt.txt").write_text("Run the container acceptance tool.")
                canary = data / "private-canary.txt"
                canary.write_text("HARMLESS_CONTROLLER_CANARY")
                (auth / "auth.json").write_text("HARMLESS_AUTH_CANARY")
                marker = output / "consumer-git-parent.txt"
                planted = work / "git"
                planted.write_text(
                    "#!/bin/sh\nprintf 'unexpected parent Git' > " + shlex.quote(str(marker)) + "\n"
                )
                planted.chmod(0o755)
                tool = work / "tool.py"
                proof = output / "tool-proof.json"
                (work / "source.txt").write_text("original")
                tool.write_text(
                    "import json, os, pathlib, socket, sys\n"
                    "work, canary, auth, proof = map(pathlib.Path, sys.argv[1:5])\n"
                    "assert pathlib.Path.cwd() == work\n"
                    "assert not any(k in os.environ for k in ('CODEX_API_KEY','OPENAI_API_KEY','GH_TOKEN','PYTHONPATH'))\n"
                    "for path in (canary, auth):\n"
                    "    try: path.read_bytes()\n"
                    "    except OSError: pass\n"
                    "    else: raise AssertionError('Tool read protected state')\n"
                    "try: socket.create_connection(('127.0.0.1', int(sys.argv[5])), timeout=1).close()\n"
                    "except OSError: pass\n"
                    "else: raise AssertionError('Tool reached the model controller')\n"
                    "try: (work / 'source.txt').write_text('changed')\n"
                    "except OSError: assert sys.argv[6] != 'deliver'\n"
                    "else: assert sys.argv[6] == 'deliver'\n"
                    "proof.write_text(json.dumps({'container_tool_passed':True}))\n"
                    "print('CONTAINER_TOOL_PASSED')\n"
                )
                for path in (home, auth, *home.rglob("*"), *auth.rglob("*")):
                    os.chown(path, user.pw_uid, user.pw_gid)
                with ResponsesServer("") as server:
                    server.command = shlex.join(
                        [
                            "/usr/bin/python3",
                            "-I",
                            str(tool),
                            str(work),
                            str(canary),
                            str(auth / "auth.json"),
                            str(proof),
                            str(server.server.server_port),
                            role,
                        ]
                    )
                    profile = permission_settings(
                        auth, work, output, writable=role == "deliver", data=data
                    )
                    profile.update(server.profile())
                    profile["forced_login_method"] = "api"
                    profile["cli_auth_credentials_store"] = "ephemeral"
                    cfg = project()
                    cfg["models"] = {"implement": "gpt-6-luna", "review": "gpt-6-luna"}
                    cfg["reasoning_effort"] = {"implement": "max", "review": "max"}
                    env = {
                        "PATH": f"/opt/nexkit/parent-bin:{work}:/usr/local/bin:/usr/bin:/bin",
                        "HOME": str(home),
                        "TMPDIR": str(output),
                        "CODEX_HOME": str(auth),
                        "CODEX_API_KEY": "harmless-native-api-canary",
                        "LANG": "C.UTF-8",
                    }
                    with (data / "prompt.txt").open("rb") as prompt:
                        result = subprocess.run(
                            cli_command(
                                cfg,
                                role,
                                output,
                                workspace=work,
                                output=output / "result.json",
                                data=data,
                                profile=profile,
                            ),
                            env=env,
                            cwd=work,
                            stdin=prompt,
                            capture_output=True,
                            text=True,
                            timeout=90,
                            user=user.pw_uid,
                            group=user.pw_gid,
                            extra_groups=[],
                        )
                    self.assertEqual(
                        result.returncode, 0, result.stdout[-4000:] + result.stderr[-2000:]
                    )
                    self.assertFalse(marker.exists(), "Consumer Git ran in the model parent")
                    self.assertEqual(server.errors, [])
                    self.assertEqual(len(server.requests), 2)
                    for request in server.requests:
                        self.assertEqual(request["model"], "gpt-6-luna")
                        self.assertEqual(request["reasoning"]["effort"], "max")
                    outputs = [
                        item
                        for item in server.requests[1]["input"]
                        if item.get("type") in {"function_call_output", "custom_tool_call_output"}
                    ]
                    self.assertTrue(
                        any("CONTAINER_TOOL_PASSED" in str(item.get("output")) for item in outputs),
                        outputs,
                    )
                    self.assertTrue(read_regular_json(proof)["container_tool_passed"])
                    self.assertEqual(
                        read_regular_json(output / "result.json")["summary"],
                        "Checked the native tool",
                    )
                    self.assertEqual(
                        (work / "source.txt").read_text(),
                        "changed" if role == "deliver" else "original",
                    )
