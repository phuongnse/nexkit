"""What the runner's `run.json` gives the engine's `step` after a plan, a stopped round and a
review: its tokens, seconds, money and session, and nothing else of it
(docs/engine.md#getting-there)."""

import contextlib
import json
import os
import tempfile
from pathlib import Path
from unittest import mock

from nexkit import autopilot, engine, host
from nexkit.report import report
from nexkit.state import PLAN_MARKER, render_run
from tests.test_autopilot import WORKFLOW, pilot
from tests.test_host import ASKED, HostCase
from tests.test_report import ReportCase

ANSWER = engine.answer
# A valid `run.json`: the runner's result for a stage that spent these.
RUN = {
    **host.AGENT,
    "output": None,
    "tokens": {"input": 7, "cached": 3, "output": 2},
    "seconds": 42,
}
# What the event carries when there is no usable `run.json`.
NONE = {"tokens": None, "seconds": None}
SPENT = {"tokens": RUN["tokens"], "seconds": 42}
# What a missing, unreadable and unfit `run.json` write, as `write` takes them.
BAD = {
    "unfit": {"tokens": "many"},
    "list": [RUN],
    "not json": "{",
}


def spending(event):
    result = event["result"]
    return {"tokens": result["tokens"], "seconds": result["seconds"]}


@contextlib.contextmanager
def finished():
    """The `actionFinished` events the engine gets in the block, as a list filled at its end."""
    events = []
    with mock.patch.object(host.engine, "answer", wraps=ANSWER) as answer:
        yield events
    events.extend(
        call.args[0]["event"]
        for call in answer.call_args_list
        if call.args[0]["event"]["kind"] == "actionFinished"
    )


def one(events, prefix):
    """The event whose id starts with `prefix`."""
    [event] = [e for e in events if e["id"].startswith(prefix)]
    return event


class AfterReviewTest(ReportCase):
    def reviewed(self, ran=None):
        """The review's event and `report`'s outcome after a round, with `ran` as the review's
        `run.json` unless it is None. Each call is a round of its own."""
        self.tearDown()
        self.setUp()
        self.candidate()
        if isinstance(ran, str):
            path = self.artifacts / "nexkit-review" / "run.json"
            path.write_text(ran)
        elif ran is not None:
            self.write("nexkit-review", "run.json", ran)
        with finished() as events:
            outcome = self.run_report()
        return one(events, "round-review-"), outcome

    def test_the_review_s_event_carries_the_runner_s_tokens_and_seconds(self):
        event, _ = self.reviewed(RUN)
        self.assertEqual(spending(event), SPENT)
        self.assertEqual(event["result"]["session"], None)

    def test_a_review_without_a_usable_file_sends_no_spending_and_report_finishes(self):
        event, outcome = self.reviewed()
        self.assertEqual(spending(event), NONE)
        for name, ran in BAD.items():
            with self.subTest(name):
                event, found = self.reviewed(ran)
                self.assertEqual(spending(event), NONE)
                self.assertEqual(found["outcome"], outcome["outcome"])

    def test_the_file_changes_nothing_but_the_spending(self):
        bare, without = self.reviewed()
        sent, found = self.reviewed(RUN)
        for key in ("output", "limit", "error", "transient", "noResult", "timedOut"):
            self.assertEqual(sent["result"][key], bare["result"][key], key)
        self.assertEqual(found["outcome"], without["outcome"])
        self.assertEqual(found["summary"], without["summary"])


class AfterPlanTest(HostCase):
    def report_plan(self, cfg, output, ran=None):
        """`report` after a plan run with this output and `ran` as its `run.json`."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        artifacts = Path(tmp.name)
        (artifacts / "nexkit-agent").mkdir()
        result = {"status": "done", "output": output, "tokens": {"input": 1, "output": 1}}
        (artifacts / "nexkit-agent" / "result.json").write_text(json.dumps(result))
        if ran is not None:
            text = ran if isinstance(ran, str) else json.dumps(ran)
            (artifacts / "nexkit-agent" / "run.json").write_text(text)
        decision = {
            "action": "plan",
            "issue": 5,
            "pr": None,
            "target": 5,
            "branch": "nexkit/issue-5",
            "base": "main",
            "head": None,
            "note": "",
            "auto": False,
            "autopilot": True,
        }
        needs = {"publish": {"outputs": {"result": json.dumps({"published": True})}}}
        self.gh.comment(5, f"{PLAN_MARKER}\n## Plan")
        env = {"GITHUB_REPOSITORY": "acme/app", "GITHUB_RUN_ID": str(100 + self.runs)}
        self.runs += 1
        with mock.patch.dict(os.environ, env):
            return report(
                self.gh,
                decision,
                cfg,
                needs,
                artifacts,
                workflow_ref=WORKFLOW,
                default_branch="main",
            )

    def planned(self, ran=None):
        """The plan's event and `report`'s result for a plan that autopilot carries on, in a
        fresh repository, with `ran` as the plan's `run.json` unless it is None."""
        self.setUp()
        self.issue(5)
        self.run_comment(5, "plan", status="running")
        cfg = pilot(answer="recommended")
        with finished() as events:
            result = self.report_plan(cfg, {"questions": [ASKED]}, ran)
        return one(events, "run-"), result

    def test_the_plan_s_event_carries_the_runner_s_tokens_and_seconds(self):
        event, _ = self.planned(RUN)
        self.assertEqual(spending(event), SPENT)

    def test_a_plan_without_a_usable_file_sends_no_spending(self):
        event, without = self.planned()
        self.assertEqual(spending(event), NONE)
        for name, ran in BAD.items():
            with self.subTest(name):
                event, result = self.planned(ran)
                self.assertEqual(spending(event), NONE)
                self.assertEqual(result, without)

    def test_the_file_changes_nothing_but_the_spending(self):
        bare, without = self.planned()
        sent, found = self.planned(RUN)
        self.assertEqual(found, without)
        for key in ("output", "limit", "error", "transient", "noResult", "timedOut"):
            self.assertEqual(sent["result"][key], bare["result"][key], key)


class StoppedTest(HostCase):
    def carries_on(self, decision, outcome, ran=RUN, **agent):
        """The stopped round's event and whether autopilot carries on, in a fresh repository.
        The item's state was stored while the round of `decision` ran, as `nexkit route` stores
        it: the decision after a stopped round reads the agent run that it waits for."""
        self.setUp()
        self.issue(5)
        cfg = pilot(answer="recommended")
        if decision["action"] == "fix":
            self.gh.add_pull(6, 5, head_sha="c" * 40)
            self.run_comment(5, "go", outcome="pr_opened")
            row = {
                "round": 2,
                "trigger": "fix",
                "url": "https://github.com/acme/app/actions/runs/9",
            }
            row.update(head="c" * 40, action="fix", status="running")
            self.gh.comment(6, render_run(row, "Running."))
        else:
            self.run_comment(5, "go", status="running")
        host.store_import(self.gh, cfg, 5)
        if decision["action"] != "fix":
            self.gh.add_pull(6, 5, head_sha="c" * 40)
        with finished() as events, mock.patch.dict(os.environ, {"GITHUB_RUN_ID": "9"}):
            carried = autopilot.carries_on(self.gh, cfg, decision, outcome, agent, ran=ran)
        return one(events, "run-"), carried

    def test_a_blocked_go_sends_the_runner_s_tokens(self):
        go = {"issue": 5, "pr": None, "action": "implement"}
        output = {"questions": [{**ASKED, "closes": 0}]}
        found = self.carries_on(go, "agent_blocked", status="blocked", output=output)
        event, carried = found
        self.assertTrue(carried)
        self.assertEqual(spending(event), SPENT)
        self.assertEqual(event["result"]["output"]["questions"][0]["closes"], 0)

    def test_a_round_that_ran_out_of_time_sends_the_runner_s_tokens(self):
        for action, pr in (("implement", None), ("fix", 6)):
            with self.subTest(action):
                decision = {"issue": 5, "pr": pr, "action": action, "head": "c" * 40}
                event, carried = self.carries_on(decision, "agent_error", timed_out=True)
                self.assertTrue(carried)
                self.assertTrue(event["result"]["timedOut"])
                self.assertEqual(spending(event), SPENT)

    def test_a_stopped_round_without_a_usable_file_sends_no_spending(self):
        go = {"issue": 5, "pr": None, "action": "implement"}
        for name, ran in (("missing", None), ("unfit", {"tokens": "many"}), ("list", [RUN])):
            with self.subTest(name):
                event, carried = self.carries_on(go, "agent_error", ran=ran, timed_out=True)
                self.assertTrue(carried)
                self.assertEqual(spending(event), NONE)
