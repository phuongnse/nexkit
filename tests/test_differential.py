"""The families of the differential check that play a round's report store the round while it
runs. The folder is no package, so test discovery never reaches it: this file loads the families
by their module names."""

import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from nexkit import host, item_state
from tests.support import FakeGitHub

ROOT = Path(__file__).resolve().parent.parent
DIFF = ROOT / "tests" / "differential"


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
