"""`nexkit-engine`: its program, its refusals and its purity (docs/engine.md#the-engine).

The engine's rules are played by tests/test_conformance.py, which also sends every request of
its cases to the program twice and checks that both answers are the same."""

import ast
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from nexkit import engine, protocol
from nexkit.engine import cli, machine

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = protocol.ROOT / "examples"
# What each module of the engine may import, and the modules of NexKit those import in turn,
# with relative imports resolved. Nothing here reads the network or the clock: `machine`
# reads nothing, `nexkit.protocol` reads only its own schemas, which ship inside the `nexkit`
# package, `nexkit.engine.cli` reads only standard input, and `nexkit.engine.serve`, the program
# around the engine as `cli` is, only the requests that come to its socket. Nothing imports
# either of those two but `cli`. Any other import fails the test.
ALLOWED = {
    "nexkit": set(),
    "nexkit.engine.machine": {"__future__", "copy", "nexkit.schema"},
    "nexkit.engine": {"__future__", "json", "nexkit.protocol", "nexkit.engine.machine"},
    "nexkit.engine.cli": {"__future__", "sys", "nexkit.engine", "nexkit.engine.serve"},
    "nexkit.engine.serve": {
        "__future__",
        "argparse",
        "signal",
        "sys",
        "traceback",
        "http.server",
        "nexkit.engine",
    },
    "nexkit.schema": {"__future__", "string"},
    "nexkit.protocol": {"__future__", "functools", "json", "pathlib", "nexkit.schema"},
}
# Built-ins that read or run something.
IMPURE_CALLS = {"open", "input", "exec", "eval", "compile", "__import__", "breakpoint"}


def created():
    return json.loads((EXAMPLES / "events" / "created.json").read_text())


def request(**fields):
    return {"protocol": 1, "state": None, "event": created(), "policies": {}, **fields}


def source_of(module):
    path = ROOT / Path(*module.split("."))
    return (path / "__init__.py" if path.is_dir() else path.with_suffix(".py")).read_text()


def imports(module, source):
    """The modules `source`, the code of `module`, imports, with relative imports resolved."""
    package = module if (ROOT / Path(*module.split("."))).is_dir() else module.rpartition(".")[0]
    found = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parent = package.rsplit(".", node.level - 1)[0] if node.level > 1 else package
                base = f"{parent}.{base}".rstrip(".")
            for alias in node.names:
                name = f"{base}.{alias.name}"
                path = ROOT / Path(*name.split("."))
                is_module = path.with_suffix(".py").exists() or path.is_dir()
                found.add(name if is_module else base)
    return found


def impurities(module, source=None):
    """What `module` imports beyond `ALLOWED`, and the same for each NexKit module it imports."""
    source = source_of(module) if source is None else source
    found = []
    for name in sorted(imports(module, source)):
        if name not in ALLOWED.get(module, set()):
            found.append(f"{module} imports {name}")
        elif name.startswith("nexkit") and name != module:
            found += impurities(name)
    calls = {
        node.func.id
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    found += [f"{module} calls {name}" for name in sorted(calls & IMPURE_CALLS)]
    return found


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
        code, answer = self.run_program("{")
        self.assertEqual(code, 1)
        self.assertEqual(protocol.check(answer, "calls/error.schema.json"), [])
        code, answer = self.run_program(json.dumps(request(policies={"fixRounds": "two"})))
        self.assertEqual(code, 1)
        self.assertIn("request.policies.fixRounds must be an integer", answer["error"])
        code, answer = self.run_program(json.dumps(request(policies={"fixRounds": -1})))
        self.assertIn("request.policies.fixRounds must be at least 0", answer["error"])

    def test_an_answer_names_the_protocol_version(self):
        with mock.patch.object(protocol, "VERSION", 9):
            self.assertEqual(self.run_program("{")[1]["protocol"], 9)
            self.assertEqual(self.run_program('{"call": "sweep"}')[1]["protocol"], 9)

    def test_a_first_event_that_is_not_created_is_refused(self):
        tick = {**json.loads((EXAMPLES / "events" / "tick.json").read_text()), "item": "42"}
        answer = engine.answer({**request(event=tick), "call": "step"})
        self.assertIn("first event", answer["error"])

    def test_an_unknown_call_is_refused(self):
        self.assertIn("error", engine.answer({**request(), "call": "dance"}))

    def test_a_failure_of_the_engine_is_an_error_answer(self):
        with mock.patch.object(engine, "step", side_effect=KeyError("status")):
            code, answer = self.run_program(json.dumps(request()))
        self.assertEqual(code, 1)
        self.assertEqual(protocol.check(answer, "calls/error.schema.json"), [])
        self.assertIn("KeyError", answer["error"])

    def test_a_review_only_item_is_never_changed_whatever_its_state_says(self):
        state = json.loads((EXAMPLES / "state" / "awaitingApproval.json").read_text())
        state["itemKind"] = "reviewOnly"
        state["plan"]["questions"] = []
        event = json.loads((EXAMPLES / "events" / "planApproved.json").read_text())
        event["plan"] = state["plan"]["text"]["ref"]
        answer = engine.answer({**request(state=state, event=event), "call": "step"})
        self.assertIn("review-only", answer["error"])

    def test_a_state_that_would_accept_a_review_only_item_is_refused(self):
        # No rule leads a review-only item to wait for a person in ready; a state built by
        # hand that does meets the last check of `step`.
        state = json.loads((EXAMPLES / "state" / "ready.json").read_text())
        state["itemKind"] = "reviewOnly"
        event = json.loads((EXAMPLES / "events" / "changeApproved.json").read_text())
        event["changeVersion"] = state["change"]["version"]
        policies = {"accept": "autoWhenPassed"}
        answer = engine.answer(
            {**request(state=state, event=event, policies=policies), "call": "step"}
        )
        self.assertIn("review-only", answer.get("error", ""), answer)

    def test_an_answer_that_writes_to_a_review_only_item_is_refused(self):
        # Rules guard each path; this is the last check before an answer leaves.
        for action in (
            {"kind": "publish", "stage": "fix"},
            {"kind": "accept"},
            {"kind": "runAgent", "stage": "plan"},
        ):
            with self.assertRaisesRegex(machine.EngineError, "review-only"):
                machine._never_writes([action])
        machine._never_writes([{"kind": "runAgent", "stage": "review"}, {"kind": "show"}])

    def test_the_defaults_are_the_protocol_s(self):
        properties = protocol.load("policies.schema.json")["properties"]
        defaults = {name: policy["default"] for name, policy in properties.items()}
        self.assertEqual(machine.DEFAULTS, defaults)
        self.assertFalse(machine.DEFAULTS["splits"])


class PurityTest(unittest.TestCase):
    def test_nothing_in_the_engine_reads_the_world(self):
        modules = ["nexkit", "nexkit.engine"] + [
            f"nexkit.engine.{path.stem}"
            for path in sorted((ROOT / "nexkit" / "engine").glob("*.py"))
            if path.name != "__init__.py"
        ]
        self.assertLessEqual(set(modules), set(ALLOWED))
        for module in modules:
            self.assertEqual(impurities(module), [], module)

    def test_an_import_that_reads_the_world_fails_the_test(self):
        source = source_of("nexkit.engine.machine")
        for line in (
            "import sys",
            "import importlib",
            "from .. import github, gitutil",
            "from ..agent import run_stage",
            "from . import cli",
            "from . import serve",
            "from .serve import Handler",
            "import os.path",
        ):
            with self.subTest(line=line):
                found = impurities("nexkit.engine.machine", f"{line}\n{source}")
                self.assertTrue(found, line)
        found = impurities("nexkit.engine.machine", f"{source}\nopen('x')\n")
        self.assertEqual(found, ["nexkit.engine.machine calls open"])


if __name__ == "__main__":
    unittest.main()
