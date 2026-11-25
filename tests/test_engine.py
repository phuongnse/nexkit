"""`nexkit-engine`: its program, its refusals and its purity (docs/engine.md#the-engine).

The engine's rules are played by tests/test_conformance.py, which also sends every request of
its cases to the program twice and checks that both answers are the same."""

import ast
import io
import json
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

import nexkit
from nexkit import engine, protocol
from nexkit.engine import cli, machine
from tests.test_conformance import AGENT

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = protocol.ROOT / "examples"
# What each module of the engine may import, and the modules of NexKit those import in turn,
# with relative imports resolved. Nothing here reads the network or the clock: `machine` and
# `textutil` read nothing, `prompt` reads only its own templates and `nexkit.protocol` only its own
# schemas, which ship inside the `nexkit` package, `nexkit.engine.cli` reads only standard
# input, and `nexkit.engine.serve`, the program around the engine as `cli` is, only the requests
# that come to its socket. Nothing imports either of those two but `cli`. Any other import
# fails the test.
ALLOWED = {
    "nexkit": set(),
    "nexkit.engine.machine": {"__future__", "copy", "nexkit.protocol.size", "nexkit.schema"},
    "nexkit.protocol.size": {"__future__", "hashlib", "json"},
    "nexkit.engine": {
        "__future__",
        "json",
        "re",
        "nexkit",
        "nexkit.protocol",
        "nexkit.engine.machine",
        "nexkit.engine.prompt",
    },
    # `importlib` only for `importlib.resources`, to read the prompts' own templates, which
    # ship inside the package as the schemas do; `nexkit.protocol` for the checklist stages.
    "nexkit.engine.prompt": {
        "__future__",
        "functools",
        "importlib",
        "re",
        "string",
        "nexkit.protocol",
        "nexkit.engine.textutil",
    },
    "nexkit.engine.textutil": {"__future__", "re"},
    "nexkit.engine.cli": {"__future__", "sys", "nexkit.engine", "nexkit.engine.serve"},
    "nexkit.engine.serve": {
        "__future__",
        "argparse",
        "signal",
        "socket",
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


class LargeRequestTest(unittest.TestCase):
    def test_a_large_request_that_breaks_the_protocol_is_refused_fast_and_briefly(self):
        # About 1 MiB, the most serve reads: a crowded outbox, and a long array of wrong items.
        start = json.loads((EXAMPLES / "state" / "planning.json").read_text())
        for field, items in (("pending", [{}] * 340_000), ("parts", [{}] * 250_000)):
            with self.subTest(field=field):
                state = {**start, field: items}
                held = json.loads((EXAMPLES / "events" / "held.json").read_text())
                held["item"] = state["item"]
                data = json.dumps(request(state=state, event=held))
                began = time.monotonic()
                answer = engine.reply("step", data.encode())
                self.assertLess(time.monotonic() - began, 5)
                self.assertLess(len(answer["error"]), 2000)
                self.assertIn(f"request.state.{field}", answer["error"])


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

    def test_describe_says_which_protocol_the_engine_speaks(self):
        out = io.StringIO()
        code = cli.main(["describe"], io.BytesIO(b'{"protocol": 1}'), out)
        answer = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(protocol.check(answer, "calls/describe.answer.schema.json"), [])
        self.assertEqual(answer["protocol"], {"major": 1, "minor": protocol.MINOR})
        self.assertEqual(answer["engine"], nexkit.__version__)
        self.assertEqual(answer["calls"], list(engine.CALLS))
        self.assertIn("describe", engine.CALLS)
        with mock.patch.object(protocol, "MINOR", 3):
            self.assertEqual(
                engine.answer({"protocol": 1, "call": "describe"})["protocol"]["minor"], 3
            )
        self.assertIn("error", engine.answer({"protocol": 2, "call": "describe"}))

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

    def test_a_review_only_item_is_never_closed_even_by_a_lost_close(self):
        # A state built by hand can hold a close that `actionLost` would issue again.
        state = json.loads((EXAMPLES / "state" / "dropped.json").read_text())
        state["itemKind"] = "reviewOnly"
        event = json.loads((EXAMPLES / "events" / "actionLost.json").read_text())
        event["action"] = "a7.1"
        event["changeVersion"] = None
        answer = engine.answer({**request(state=state, event=event), "call": "step"})
        self.assertIn("review-only", answer.get("error", ""), answer)
        with self.assertRaisesRegex(machine.EngineError, "review-only"):
            machine._never_writes([{"kind": "close"}])

    def test_an_action_withdrawn_in_a_step_leaves_its_id_unused(self):
        work = machine.Step(machine.blank("42"), {}, protocol.check)
        first = work.do("runChecks", changeVersion="v1")
        work.do("show", what="plan", text={"name": "plan", "ref": "a1.1"})
        work.withdraw(work.s["pending"][0])
        work.do("runChecks", changeVersion="v2")
        ids = [action["id"] for action in work.actions]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertNotIn(first["id"], ids)

    def test_a_move_withdraws_a_notice_given_earlier_in_the_step(self):
        # No rule moves on after a notice in the same step today; `move` keeps it so.
        work = machine.Step(machine.blank("42"), {}, protocol.check)
        work.need("approval", "The plan waits for its approval.", "a1.1")
        work.move("checking")
        self.assertEqual([a for a in work.actions if a["kind"] == "needPerson"], [])
        self.assertEqual(work.s["pending"], [])

    def test_the_defaults_are_the_protocol_s(self):
        properties = protocol.load("policies.schema.json")["properties"]
        defaults = {name: policy["default"] for name, policy in properties.items()}
        self.assertEqual(machine.DEFAULTS, defaults)
        self.assertFalse(machine.DEFAULTS["splits"])

    def test_the_answers_and_split_policies_are_refused_outside_their_values(self):
        cases = [
            ({"answers": "always"}, "request.policies.answers must be one of"),
            ({"splitParts": 11}, "request.policies.splitParts must be at most 10"),
            ({"splitParts": 0}, "request.policies.splitParts must be at least 1"),
            ({"splitDepth": 0}, "request.policies.splitDepth must be at least 1"),
        ]
        for policies, found in cases:
            with self.subTest(policies=policies):
                answer = engine.answer({**request(policies=policies), "call": "step"})
                self.assertIn(found, answer["error"])

    def test_created_gives_the_title_and_depth_and_a_request_gives_them_again(self):
        event = {**created(), "title": "T" * 300, "depth": 2}
        state = engine.answer({**request(event=event), "call": "step"})["state"]
        self.assertEqual((state["title"], state["depth"]), ("T" * 256, 2))
        again = {
            **json.loads((EXAMPLES / "events" / "requested.json").read_text()),
            "item": state["item"],
            "what": "plan",
            "title": "Another title",
            "depth": 1,
        }
        found = engine.answer({**request(state=state, event=again), "call": "step"})
        self.assertEqual((found["state"]["title"], found["state"]["depth"]), ("Another title", 1))
        # The state keeps no more of a title than GitHub allows, and no depth below 0.
        for name, value, problem in (
            ("title", "T" * 257, "value.title has more than 256 characters"),
            ("depth", -1, "value.depth must be at least 0"),
        ):
            with self.subTest(name=name):
                broken = {**state, name: value}
                self.assertIn(problem, protocol.check(broken, "state.schema.json"))
        answer = engine.answer({**request(event={**created(), "depth": -1}), "call": "step"})
        self.assertIn("request.event.depth must be at least 0", answer["error"])


class RetryTimeTest(unittest.TestCase):
    def test_a_wait_is_added_to_the_time_of_the_event(self):
        for at, minutes, found in (
            ("2026-11-23T15:01:11Z", 15, "2026-11-23T15:16:11Z"),
            ("2026-12-31T23:59:59Z", 2, "2027-01-01T00:01:59Z"),
            ("2024-02-28T23:30:00Z", 60, "2024-02-29T00:30:00Z"),
            ("2100-02-28T23:59:00Z", 1, "2100-03-01T00:00:00Z"),
            ("2026-11-23T15:01:11.5+02:00", 30, "2026-11-23T13:31:11Z"),
            ("2026-11-23T01:00:00-05:30", 330, "2026-11-23T12:00:00Z"),
            ("2026-11-23T15:01:11", 0, "2026-11-23T15:01:11Z"),
        ):
            with self.subTest(at=at):
                self.assertEqual(machine._later(at, minutes), found)

    def test_a_time_that_is_not_iso_8601_is_refused(self):
        for at in ("soon", "2026-02-30T00:00:00Z", "2026-11-23", "2026-11-23T25:00:00Z", ""):
            with self.subTest(at=at):
                with self.assertRaisesRegex(machine.EngineError, "not an ISO 8601 time"):
                    machine._later(at, 15)

    def test_a_transient_stop_at_an_unreadable_time_is_refused(self):
        state = json.loads((EXAMPLES / "state" / "planning.json").read_text())
        event = json.loads((EXAMPLES / "events" / "actionFinished.json").read_text())
        event.update(item=state["item"], action=state["pending"][0]["id"], at="soon")
        event["changeVersion"] = state["pending"][0].get("changeVersion")
        event["result"] = {**AGENT, "output": None, "transient": True, "error": "Overloaded."}
        answer = engine.answer({**request(state=state, event=event), "call": "step"})
        self.assertIn("not an ISO 8601 time", answer.get("error", ""), answer)


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
