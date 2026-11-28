"""`--forbid` of the differential harness (`tests/differential/harness.py`) counts a fallback of a
named host function as a problem, and the families that play a round's report store the round
while it runs. The folder is no package, so test discovery never reaches it: this file loads the
harness by its path, as `tests/test_mutants.py` loads the mutation check, and the families by
their module names."""

import contextlib
import importlib
import importlib.util
import io
import os
import sys
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from nexkit import host, item_state
from tests.support import FakeGitHub

ROOT = Path(__file__).resolve().parent.parent
DIFF = ROOT / "tests" / "differential"
HARNESS = DIFF / "harness.py"


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


def spying(name, seen):
    """Patch `host.<name>` so that it notes the item's stored state when it starts, then runs."""
    real = getattr(host, name)

    def spy(gh, cfg, decision, *args, **kwargs):
        seen.append(item_state.read(gh, decision["issue"])[1])
        return real(gh, cfg, decision, *args, **kwargs)

    return mock.patch.object(host, name, spy)


class StoredRoundTest(unittest.TestCase):
    """The families that play a round's report store the item's state just after the round's
    `progress.start`, and their scripted agents give output that fits the stage, so that the
    report stores the agent and publish results before `round_ended` or `conflicting` decides.
    Without it, the stored state holds no `rounds.fixed` and no pending `publish`."""

    def setUp(self):
        self.enterContext(mock.patch.object(sys, "path", [str(DIFF), *sys.path]))
        self.enterContext(
            mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "acme/app", "GITHUB_RUN_ID": "4242"})
        )
        missing = object()
        wrapped = {
            name: vars(FakeGitHub).get(name, missing)
            for name in ("comment", "update_comment", "_nexkit_recording")
        }
        before = set(sys.modules)
        self.common = importlib.import_module("common")
        self.addCleanup(self.restore, before, wrapped, missing)

    def restore(self, before, wrapped, missing):
        """Forget the families and `common`, what they recorded and the wrapping of the fake
        that `common.seed` makes for its whole class."""
        self.common.reset()
        for name in set(sys.modules) - before:
            if name in ("common", "families") or name.startswith("families."):
                sys.modules.pop(name)
        for name, value in wrapped.items():
            if value is missing:
                if name in vars(FakeGitHub):
                    delattr(FakeGitHub, name)
            else:
                setattr(FakeGitHub, name, value)

    def play(self, family, pick, spied):
        """The item's stored state each time `host.<spied>` started in the first scenario of
        `family` that `pick` accepts."""
        module = importlib.import_module(f"families.{family}")
        scenario = next(sc for sc in module.scenarios() if pick(sc))
        seen = []
        with spying(spied, seen):
            module.run(scenario)
        self.assertTrue(seen, f"{family} never started host.{spied}")
        return seen[0]

    def published(self, state):
        """The `runChecks` of the stored state's outbox."""
        return [item for item in state["pending"] if item["kind"] == "runChecks"]

    def assert_fixed(self, state):
        found = self.published(state)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["changeVersion"], self.common.HEAD)
        self.assertEqual(state["rounds"]["fixed"], self.common.HEAD)

    def test_a_fix_round_of_the_report_family_is_stored_before_its_checks(self):
        state = self.play(
            "report",
            lambda sc: (
                sc["action"] == "fix_person"
                and sc["checks"] == "pass"
                and sc["review"] == "approve"
                and not sc["recheck"]
                and sc["prior"] is None
                and sc["hold"] == "no"
                and sc["base"] == "no"
                and not sc["ap"]
            ),
            "round_ended",
        )
        self.assert_fixed(state)

    def test_an_implement_round_of_the_report_family_is_in_its_checks(self):
        state = self.play(
            "report",
            lambda sc: (
                sc["action"] == "implement"
                and sc["checks"] == "pass"
                and sc["review"] == "approve"
                and sc["prior"] is None
                and sc["hold"] == "no"
                and sc["base"] == "no"
                and not sc["ap"]
            ),
            "round_ended",
        )
        found = self.published(state)
        self.assertTrue(found)
        self.assertEqual(found[0]["changeVersion"], self.common.HEAD)

    def test_a_fix_round_of_the_round_family_is_stored_before_its_checks(self):
        state = self.play(
            "round",
            lambda sc: (
                sc["prior"] == "none"
                and sc["action"] == "fix_person"
                and sc["checks"] is True
                and sc["review"] == "approve"
                and not sc["recheck"]
                and not sc["hold"]
                and not sc["ap"]
            ),
            "round_ended",
        )
        self.assert_fixed(state)

    def test_a_fix_round_of_the_review_family_is_stored_before_its_checks(self):
        state = self.play(
            "review",
            lambda sc: (
                sc["kind"] == "fix_person"
                and sc["checks"] == "pass"
                and sc["review"] == "approve"
                and sc["prev"] == "none"
                and not sc["ap"]
            ),
            "round_ended",
        )
        self.assert_fixed(state)

    def test_a_conflicting_fix_round_leaves_its_publish_pending(self):
        state = self.play(
            "conflict",
            lambda sc: (
                sc["kind"] == "person"
                and sc["earlier"] == 0
                and sc["last"] == "ended"
                and sc["hold"] == "no"
                and sc["after"] == "none"
            ),
            "conflicting",
        )
        publishing = [item for item in state["pending"] if item["kind"] == "publish"]
        self.assertEqual([item["stage"] for item in publishing], ["fix"])


if __name__ == "__main__":
    unittest.main()
