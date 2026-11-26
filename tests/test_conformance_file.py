"""scripts/conformance.py: the suite as one file of whole requests and exact answers
(docs/engine.md#conformance), and `check`, which plays such a file against an engine."""

import contextlib
import copy
import importlib.util
import io
import json
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

from nexkit import __version__, protocol
from tests.test_conformance import CASES, run_program
from tests.test_serve import server

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("conformance", ROOT / "scripts" / "conformance.py")
conformance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(conformance)

REFUSAL = "a-replace-result-that-observes-another-change-is-refused"
PROGRAM = f"{shlex.quote(sys.executable)} {shlex.quote(str(ROOT / 'bin' / 'nexkit-engine'))}"


def expected_names():
    """The cases the folders hold: `fixtures.json` is not one, and the imports stay out."""
    names = {path.stem for path in CASES.glob("*.json") if path.name != "fixtures.json"}
    return names | {f"sweeps/{path.stem}" for path in (CASES / "sweeps").glob("*.json")}


def missing(document):
    """The cases of the folders that the file leaves out."""
    return sorted(expected_names() - {case["name"] for case in document["cases"]})


def run(*argv):
    out = io.StringIO()
    return conformance.main(list(argv), out), out.getvalue()


class ConformanceFileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.path = Path(cls.directory.name) / "suite.json"
        conformance.build(cls.path)
        cls.document = json.loads(cls.path.read_text(encoding="utf-8"))

    def write(self, name, document):
        path = Path(self.directory.name) / name
        path.write_text(json.dumps(document), encoding="utf-8")
        return str(path)

    def trimmed(self):
        """A few step cases, one of them a refusal, and one sweep case."""
        cases = {case["name"]: case for case in self.document["cases"]}
        steps = [c for c in self.document["cases"] if not c["name"].startswith("sweeps/")]
        names = [steps[0]["name"], steps[1]["name"], REFUSAL]
        sweep = next(name for name in cases if name.startswith("sweeps/"))
        return copy.deepcopy({**self.document, "cases": [cases[name] for name in [*names, sweep]]})

    def changed(self, document):
        """A copy of `document` whose first request has a wrong answer."""
        document = copy.deepcopy(document)
        document["cases"][0]["requests"][0]["answer"] += " "
        return document

    def test_the_file_fits_its_schema_and_names_the_versions(self):
        self.assertEqual(protocol.check(self.document, "conformance.schema.json"), [])
        self.assertEqual(self.document["protocol"], {"major": protocol.VERSION, "minor": 0})
        self.assertEqual(self.document["protocol"]["minor"], protocol.MINOR)
        self.assertEqual(self.document["nexkit"], __version__)

    def test_the_file_holds_every_case_of_the_folders(self):
        self.assertEqual(missing(self.document), [])
        names = [case["name"] for case in self.document["cases"]]
        self.assertEqual(sorted(names), sorted(expected_names()))
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(any(name.startswith("sweeps/") for name in names))
        self.assertFalse(any(name.startswith("imports/") for name in names))

    def test_the_coverage_check_names_a_case_that_was_removed(self):
        document = copy.deepcopy(self.document)
        removed = document["cases"].pop(5)["name"]
        self.assertEqual(missing(document), [removed])
        sweep = next(i for i, c in enumerate(document["cases"]) if c["name"].startswith("sweeps/"))
        removed_sweep = document["cases"].pop(sweep)["name"]
        self.assertEqual(missing(document), sorted([removed, removed_sweep]))

    def test_each_step_answer_is_the_text_the_program_gives(self):
        refusals = 0
        for case in self.document["cases"]:
            if case["name"].startswith("sweeps/"):
                continue
            for entry in case["requests"]:
                self.assertEqual(entry["call"], "step")
                self.assertEqual(run_program(entry["request"]), entry["answer"], case["name"])
                self.assertTrue(entry["answer"].endswith("\n"))
                refusals += "error" in json.loads(entry["answer"])
        self.assertGreater(refusals, 0)

    def test_the_tree_engine_passes_the_whole_file_over_http(self):
        with server() as port:
            code, text = run("check", str(self.path), "--url", f"http://127.0.0.1:{port}")
        self.assertEqual((code, text), (0, f"{self.count(self.document)} answers match\n"))

    def test_the_tree_engine_passes_a_trimmed_file_on_standard_input(self):
        document = self.trimmed()
        self.assertIn(REFUSAL, [case["name"] for case in document["cases"]])
        code, text = run("check", self.write("trimmed.json", document), "--program", PROGRAM)
        self.assertEqual((code, text), (0, f"{self.count(document)} answers match\n"))

    def test_a_changed_answer_is_named_over_http_and_on_standard_input(self):
        document = self.changed(self.trimmed())
        path = self.write("changed.json", document)
        name = document["cases"][0]["name"]
        with server() as port:
            code, text = run("check", path, "--url", f"http://127.0.0.1:{port}")
        self.assertEqual(code, 1)
        self.assertIn(f"{name} request 1 (step): the answer differs", text)
        self.assertIn(f"1 of {self.count(document)} answers differ", text)
        code, text = run("check", path, "--program", PROGRAM)
        self.assertEqual(code, 1)
        self.assertIn(f"{name} request 1 (step): the answer differs", text)

    def test_a_refusal_with_other_words_differs(self):
        document = self.trimmed()
        case = next(c for c in document["cases"] if c["name"] == REFUSAL)
        entry = next(e for e in case["requests"] if "error" in json.loads(e["answer"]))
        error = json.loads(entry["answer"])
        entry["answer"] = json.dumps({**error, "error": "Other words."}, sort_keys=True) + "\n"
        with server() as port:
            code, text = run(
                "check", self.write("words.json", document), "--url", f"http://127.0.0.1:{port}"
            )
        self.assertEqual(code, 1)
        self.assertIn(f"{REFUSAL} request", text)

    def test_the_status_of_an_answer_is_checked_too(self):
        entry = {"call": "step", "answer": '{"error": "x", "protocol": 1}\n'}
        self.assertIsNone(conformance.difference(entry, 400, entry["answer"], None, (200, 400)))
        self.assertEqual(
            conformance.difference(entry, 200, entry["answer"], None, (200, 400)),
            "status 200, expected 400",
        )
        self.assertEqual(
            conformance.difference(entry, 0, entry["answer"], None, (0, 1)),
            "status 0, expected 1",
        )

    def test_an_engine_that_does_not_answer_is_reported_not_raised(self):
        document = self.trimmed()
        path = self.write("gone.json", document)
        code, text = run("check", path, "--url", "http://127.0.0.1:1")
        self.assertEqual(code, 1)
        self.assertIn("no answer", text)
        code, text = run("check", path, "--program", "/nonexistent/engine")
        self.assertEqual(code, 1)
        self.assertIn("no answer", text)

    def test_a_file_that_breaks_the_schema_is_refused(self):
        path = self.write("broken.json", {"protocol": {"major": 2, "minor": 0}, "cases": []})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, text = run("check", path, "--program", PROGRAM)
        self.assertEqual((code, text), (2, ""))
        self.assertIn("nexkit", err.getvalue())

    def count(self, document):
        return sum(len(case["requests"]) for case in document["cases"])


if __name__ == "__main__":
    unittest.main()
