"""`nexkit-engine`: its program, its refusals and its purity (docs/engine.md#the-engine)."""

import ast
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path

from nexkit import engine
from nexkit.engine import cli

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "nexkit" / "protocol" / "v1" / "examples"
# Modules whose use reads the network, the clock, the environment or files, or starts programs.
IMPURE = {
    "datetime",
    "glob",
    "http",
    "io",
    "os",
    "pathlib",
    "random",
    "shutil",
    "socket",
    "subprocess",
    "tempfile",
    "time",
    "urllib",
}
# The program around the engine: it reads standard input or listens on a socket and calls the
# pure engine, which never imports it back.
PROGRAM = {"cli.py", "serve.py"}


def created():
    return json.loads((EXAMPLES / "events" / "created.json").read_text())


def request(**fields):
    return {"protocol": 1, "state": None, "event": created(), "policies": {}, **fields}


class ProgramTest(unittest.TestCase):
    def run_program(self, text):
        out = io.StringIO()
        code = cli.main(["step"], io.BytesIO(text.encode()), out)
        return code, json.loads(out.getvalue())

    def test_the_same_request_gives_the_same_answer(self):
        text = json.dumps(request())
        first, second = self.run_program(text), self.run_program(text)
        self.assertEqual(first, second)
        self.assertEqual(first[0], 0)
        self.assertEqual(first[1]["state"]["state"], "new")

    def test_the_program_in_bin_reads_standard_input(self):
        done = subprocess.run(
            [sys.executable, str(ROOT / "bin" / "nexkit-engine"), "step"],
            input=json.dumps(request()),
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(json.loads(done.stdout)["state"]["revision"], 1)

    def test_a_request_that_is_not_json_or_breaks_the_protocol_is_refused(self):
        self.assertEqual(self.run_program("{")[0], 1)
        code, answer = self.run_program(json.dumps(request(policies={"fixRounds": "two"})))
        self.assertEqual(code, 1)
        self.assertIn("request.policies.fixRounds must be an integer", answer["error"])

    def test_a_first_event_that_is_not_created_is_refused(self):
        tick = {"protocol": 1, "kind": "tick", "item": "42", "at": "2026-11-23T15:01:11Z"}
        answer = engine.answer({**request(event={**tick, "wait": "w1"}), "call": "step"})
        self.assertIn("first event", answer["error"])

    def test_an_unknown_call_is_refused(self):
        self.assertIn("error", engine.answer({**request(), "call": "dance"}))


class PurityTest(unittest.TestCase):
    def test_nothing_in_the_engine_reads_the_world(self):
        for path in (ROOT / "nexkit" / "engine").glob("*.py"):
            if path.name in PROGRAM:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module]
                elif isinstance(node, ast.ImportFrom):
                    # Relative, at any level; an absolute `nexkit...` import fails below.
                    found = {*(node.module or "").split("."), *(a.name for a in node.names)}
                    self.assertFalse(
                        {f"{name}.py" for name in found} & PROGRAM, f"{path.name} imports {found}"
                    )
                    continue
                else:
                    continue
                for name in names:
                    self.assertNotIn(name.split(".")[0], IMPURE, f"{path.name} imports {name}")
                self.assertFalse(
                    any(name.split(".")[0] == "nexkit" for name in names),
                    f"{path.name} imports {names}; import inside the package relatively",
                )
            calls = {
                node.func.id
                for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            }
            self.assertNotIn("open", calls, path.name)
            self.assertNotIn("input", calls, path.name)


if __name__ == "__main__":
    unittest.main()
