"""`--forbid` of the differential harness (`tests/differential/harness.py`) counts a fallback of a
named host function as a problem. The folder is no package, so test discovery never reaches it:
this file loads the harness by its path, as `tests/test_mutants.py` loads the mutation check."""

import contextlib
import importlib.util
import io
import sys
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tests" / "differential" / "harness.py"


def load():
    """The harness as a module. It puts its folder on `sys.path` and imports `families` and
    `known`, which the test removes again."""
    before = set(sys.modules)
    with mock.patch.object(sys, "path", list(sys.path)):
        spec = importlib.util.spec_from_file_location("nexkit_harness", HARNESS)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    for name in ("families", "known"):
        if name not in before:
            sys.modules.pop(name, None)
    return module


harness = load()


def report(forbid, fell):
    """What `harness.report` returns for a family of one scenario that differs nowhere and fell
    back as `fell` says, and what it printed."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        found = harness.report(
            "command", 1, Counter(), Counter(), {}, 0, Counter(fell), 1, 0, forbid=forbid
        )
    return found, out.getvalue()


class ForbidTests(unittest.TestCase):
    FELL = {("nexkit.host.command", "differs"): 2}

    def test_a_fallback_of_a_forbidden_function_is_a_problem(self):
        found, text = report(["nexkit.host.command"], self.FELL)
        self.assertEqual(found, 2)
        self.assertIn("forbidden fallback: 2 × nexkit.host.command (differs)", text)

    def test_a_fallback_of_another_function_is_none(self):
        found, text = report(["nexkit.host.after_plan"], self.FELL)
        self.assertEqual(found, 0)
        self.assertNotIn("forbidden fallback", text)

    def test_without_forbid_a_fallback_is_no_problem(self):
        self.assertEqual(report((), self.FELL)[0], 0)

    def test_every_fallback_of_the_function_counts(self):
        fell = {**self.FELL, ("nexkit.host.command", "no state"): 3}
        self.assertEqual(report(["nexkit.host.command"], fell)[0], 5)

    def test_a_name_that_is_no_function_of_the_tree_is_refused(self):
        argv = ["--v6", "unused", "--forbid", "nexkit.host.no_such"]
        with (
            contextlib.redirect_stderr(io.StringIO()) as err,
            self.assertRaises(SystemExit) as stop,
        ):
            harness.main(argv)
        self.assertEqual(stop.exception.code, 2)
        self.assertIn("nexkit.host.no_such is not a function", err.getvalue())

    def test_the_forbidden_function_of_check_sh_is_a_function_of_the_tree(self):
        text = (ROOT / "tests" / "differential" / "check.sh").read_text()
        self.assertIn("--forbid nexkit.host.close_done", text)
        source = (ROOT / "nexkit" / "host.py").read_text()
        self.assertRegex(source, r"(?m)^def close_done\(")


if __name__ == "__main__":
    unittest.main()
