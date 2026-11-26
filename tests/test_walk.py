"""A fixed sample of the engine's random walks (`tests/walk.py`), and a check that it sees a
broken rule. The script plays longer walks and `tests/mutants.py` breaks the engine for it."""

import functools
import random
import unittest
from unittest import mock

from nexkit import protocol
from nexkit.engine import machine
from tests import walk

# Walks of the fixed seed 349, a few seconds: some of them take an application's policies.
WALKS = 60


@functools.cache
def sample():
    return walk.sample(WALKS)


def report(found):
    """The problems of a sample, each with the first walk that broke it."""
    lines = []
    for name, count in found["problems"].most_common(5):
        info = found["examples"][name]
        where = f"walk {info['walk']} step {info['step']}: {info['trail']}"
        lines.append(f"{count} × {name}\n  {where}")
    return "\n".join(lines)


class SampleTests(unittest.TestCase):
    def test_no_invariant_that_known_does_not_list_breaks(self):
        found = sample()
        self.assertEqual(dict(found["problems"]), {}, report(found))

    def test_the_engine_refuses_few_of_the_events(self):
        found = sample()
        self.assertGreater(found["events"], WALKS)
        self.assertLessEqual(found["refused"] / found["events"], walk.REFUSED)

    def test_some_walks_take_an_applications_policies(self):
        self.assertGreaterEqual(sample()["applied"], 1)

    def test_an_applications_policies_are_those_of_the_page_and_fit_the_schema(self):
        policies = walk.Walker.application_policies(random.Random(1))
        for name, value in {
            "planApproval": "person",
            "answers": "never",
            "reviewAfterFailingChecks": "onlyAfterPassing",
            "accept": "autoWhenPassed",
            "conflicts": "redo",
            "splits": True,
        }.items():
            self.assertEqual(policies[name], value, name)
        self.assertIn("item", policies["budgets"])
        self.assertEqual(protocol.check(policies, "policies.schema.json", "policies"), [])

    def test_an_application_walk_draws_its_policies_once(self):
        with walk.Walker() as walker:
            drawn = mock.Mock(wraps=walker.policies)
            with mock.patch.object(walker, "policies", drawn):
                walker.play(0, random.Random(1), 40, False, application=True)
                self.assertEqual(drawn.call_count, 0)
                walker.play(0, random.Random(1), 40, False)
                self.assertGreaterEqual(drawn.call_count, 1)

    def test_an_application_walk_comes_after_a_share_of_the_walks(self):
        with walk.Walker() as walker:
            for w in range(40):
                plays = []
                played = mock.patch.object(
                    walker, "play", lambda *args, plays=plays, **kw: plays.append(kw)
                )
                with played:
                    walker.walk(w, 349, 40)
                more = random.Random(f"application-349-{w}").random() < walk.APPLICATION
                want = [{}, {"application": True}] if more else [{}]
                self.assertEqual(plays, want, w)

    def test_the_sample_sees_an_item_budget_that_stops_nothing(self):
        # The rule of the mutant `item-budget-spent-run-starts` of `tests/mutants.py`, broken in
        # this process: no run stops for the item's own budget.
        with mock.patch.object(machine.Step, "stop_for_budget", lambda step: False):
            found = walk.sample(WALKS)
        self.assertIn("budget: a implement run starts on a spent budget", found["problems"])


if __name__ == "__main__":
    unittest.main()
