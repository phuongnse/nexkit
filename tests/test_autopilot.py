import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from nexkit import autopilot
from nexkit.maintain import maintain
from nexkit.report import report
from nexkit.route import route
from nexkit.state import PLAN_MARKER, render_run
from tests.support import FakeGitHub, make_config

WORKFLOW = "acme/app/.github/workflows/nexkit.yml@refs/heads/main"


def pilot(**settings):
    return make_config(autopilot={"enabled": True, **settings})


class AutopilotCase(unittest.TestCase):
    """Issues in milestone 1, and helpers that leave the comments NexKit's runs leave."""

    def setUp(self):
        self.gh = FakeGitHub()
        self.cfg = pilot()
        self.runs = 0

    def issue(self, number, **kw):
        kw.setdefault("milestone", 1)
        issue = self.gh.add_issue(number, title=f"Issue {number}", **kw)
        if not self.gh.open_milestones:
            self.gh.add_milestone(1)
        return issue

    def run_comment(self, number, command, status="success", **fields):
        self.runs += 1
        url = f"https://github.com/acme/app/actions/runs/{self.runs}"
        data = {"url": url, "command": command, "status": status, **fields}
        return self.gh.comment(number, render_run(data, "Done."))

    def planned(self, number, **fields):
        self.run_comment(number, "plan", outcome="planned", **fields)
        return self.gh.comment(number, f"{PLAN_MARKER}\n## Plan\n\nAdd it.")

    def sweep(self, cfg=None, closed=None):
        return autopilot.sweep(self.gh, cfg or self.cfg, "nexkit.yml", "main", closed)

    def started(self):
        """(command, number) for each command dispatched so far."""
        return [
            (d["inputs"]["command"], int(d["inputs"]["number"]))
            for d in self.gh.dispatches
            if d["inputs"]["command"] != "autopilot"
        ]


class SweepTests(AutopilotCase):
    def test_plans_the_oldest_ready_issue_and_not_one_that_is_blocked(self):
        self.issue(3)
        self.issue(4)
        self.issue(9, milestone=2)  # outside the current milestone
        self.gh.add_milestone(2)
        self.gh.blockers[3] = [4]
        self.sweep()
        self.assertEqual(self.started(), [("plan", 4)])
        [dispatch] = self.gh.dispatches
        self.assertEqual(dispatch["inputs"]["auto"], "autopilot")
        self.assertEqual(dispatch["ref"], "main")
        [record] = self.gh.comments(4)
        self.assertIn("NexKit autopilot started `/nexkit plan`", record["body"])

    def test_issues_without_a_milestone_or_label_are_never_taken(self):
        self.issue(3, milestone=None)
        self.gh.add_milestone(1)
        self.sweep()
        self.assertEqual(self.started(), [])
        self.issue(4, labels=["autopilot"])
        self.sweep(pilot(scope="label", label="autopilot"))
        self.assertEqual(self.started(), [("plan", 4)])

    def test_the_milestone_with_the_earliest_due_date_comes_first(self):
        self.gh.add_milestone(1)
        self.gh.add_milestone(2, due_on="2026-11-01T00:00:00Z")
        self.gh.add_milestone(3)  # no issues
        self.issue(5, milestone=1)
        self.issue(6, milestone=2)
        self.sweep()
        self.assertEqual(self.started(), [("plan", 6)])

    def test_a_finished_milestone_stays_current_until_a_person_closes_it(self):
        self.issue(5, state="closed", reason="completed")
        self.gh.add_milestone(2)
        self.issue(6, milestone=2)
        lines = self.sweep(closed=5)
        self.assertEqual(self.started(), [])
        [note] = self.gh.comments_matching(5, autopilot.DONE)
        self.assertIn("finished milestone M1", note["body"])
        self.assertIn("Close the milestone", note["body"])
        self.assertIn("Autopilot finished milestone M1.", lines)
        self.sweep(closed=5)
        self.assertEqual(len(self.gh.comments_matching(5, autopilot.DONE)), 1)
        self.gh.open_milestones.pop(0)  # a person closed it
        self.sweep()
        self.assertEqual(self.started(), [("plan", 6)])

    def test_the_done_note_mentions_the_people_to_notify(self):
        self.issue(5, state="closed", reason="completed")
        self.sweep(pilot(), closed=5)
        cfg = make_config(autopilot={"enabled": True}, notify=["olivia"])
        self.gh.issue_comments[5].clear()
        self.sweep(cfg, closed=5)
        [note] = self.gh.comments_matching(5, autopilot.DONE)
        self.assertIn("@olivia NexKit autopilot finished", note["body"])

    def test_takes_only_issues_opened_by_people_who_can_write(self):
        self.issue(3, user="bob")  # read access
        self.issue(4, user="mallory")  # no access
        self.issue(5, user="github-actions[bot]")
        _, steps = autopilot.preview(self.gh, self.cfg)
        self.assertEqual([s["state"] for s in steps], ["waits"] * 3)
        self.assertIn("@bob, who cannot write", steps[0]["reason"])
        self.sweep()
        self.assertEqual(self.started(), [])

    def test_starts_go_on_a_plan_that_needs_nobody(self):
        self.issue(5)
        self.planned(5)
        self.sweep()
        self.assertEqual(self.started(), [("go", 5)])

    def test_waits_for_a_person_when_the_plan_needs_one(self):
        self.issue(5)
        self.planned(5, attention="the plan has questions")
        _, [step] = autopilot.preview(self.gh, self.cfg)
        self.assertEqual((step["state"], step["reason"]), ("waits", "the plan has questions"))
        self.sweep()
        self.assertEqual(self.started(), [])

    def test_plans_again_after_collaborators_commented_on_the_plan(self):
        self.issue(5)
        self.planned(5)
        self.gh.human_comment(5, "/nexkit plan")  # a command is not a change
        self.gh.human_comment(5, "Hi", login="eve", association="NONE")
        self.sweep()
        self.assertEqual(self.started(), [("go", 5)])
        self.gh.dispatches.clear()
        self.planned(5)
        self.gh.human_comment(5, "Use port 8080.")
        self.sweep()
        self.assertEqual(self.started(), [("plan", 5)])

    def test_two_sweeps_start_each_command_once(self):
        self.issue(5)
        self.sweep()
        self.sweep()
        self.assertEqual(self.started(), [("plan", 5)])
        # The run starts and posts its comment: still nothing new while it runs.
        self.run_comment(5, "plan", status="running")
        self.sweep()
        self.assertEqual(self.started(), [("plan", 5)])
        # Once it planned, the next step starts, once.
        self.gh.issue_comments[5].pop()
        self.planned(5)
        self.sweep()
        self.sweep()
        self.assertEqual(self.started(), [("plan", 5), ("go", 5)])
        self.assertEqual(len(self.gh.comments_matching(5, "NexKit autopilot started")), 1)

    def test_a_command_that_never_ran_is_not_started_again(self):
        self.issue(5)
        self.sweep()
        later = datetime.now(UTC) + timedelta(hours=7)
        _, [step] = autopilot.preview(self.gh, self.cfg)
        self.assertEqual(step["state"], "active")
        step = autopilot.assess(self.gh, self.cfg, self.gh.issues[5], {}, now=later)
        self.assertEqual(step["state"], "waits")
        self.assertIn("did not run", step["reason"])

    def test_leaves_issues_with_a_run_or_an_open_pull_request_alone(self):
        self.issue(4)
        self.run_comment(4, "plan", status="paused")
        self.issue(5)
        self.planned(5)
        self.run_comment(5, "go", outcome="ready")
        self.gh.add_pull(6, 5)
        self.sweep()
        self.assertEqual(self.started(), [])

    def test_a_failed_go_or_a_closed_pull_request_waits_for_a_person(self):
        self.issue(4)
        self.planned(4)
        self.run_comment(
            4, "go", status="failure", outcome="agent_error", attention="the run stopped"
        )
        self.issue(5)
        self.planned(5)
        self.run_comment(5, "go", outcome="ready")  # its pull request was closed since
        _, steps = autopilot.preview(self.gh, self.cfg)
        self.assertEqual(
            [s["reason"] for s in steps],
            ["the run stopped", "its pull request was closed; a person decides"],
        )

    def test_max_parallel_limits_the_issues_in_progress(self):
        for number in (3, 4, 5):
            self.issue(number)
        self.planned(3)
        self.run_comment(3, "go", outcome="ready")
        self.gh.add_pull(10, 3)
        self.sweep(pilot(max_parallel=1))
        self.assertEqual(self.started(), [])
        self.sweep(pilot(max_parallel=2))
        self.assertEqual(self.started(), [("plan", 4)])
        self.gh.dispatches.clear()
        for number in (6, 7):
            self.issue(number)
        self.sweep(pilot())  # no limit
        self.assertEqual(self.started(), [("plan", 5), ("plan", 6), ("plan", 7)])

    def test_sub_issues_go_in_their_parents_order(self):
        self.issue(2)
        self.issue(3)
        self.issue(4)
        self.gh.parents.update({3: 2, 4: 2})
        _, steps = autopilot.preview(self.gh, self.cfg)
        self.assertEqual([s["state"] for s in steps], ["waits", "start", "waits"])
        self.assertIn("a parent issue", steps[0]["reason"])
        self.assertIn("waits for #3, an earlier part of #2", steps[2]["reason"])


class TriggerTests(AutopilotCase):
    def maintain(self, decision, cfg=None):
        return maintain(self.gh, decision, cfg or self.cfg, workflow="nexkit.yml", ref="main")

    def sweeps(self):
        return [d["inputs"] for d in self.gh.dispatches if d["inputs"]["command"] == "autopilot"]

    def test_a_closed_issue_starts_a_sweep_that_knows_it(self):
        self.issue(5, state="closed")
        self.maintain({"action": "maintain", "task": "parents", "issue": 5})
        self.assertEqual(self.sweeps(), [{**autopilot.SWEEP, "note": "5"}])
        decision = route(self.gh, "workflow_dispatch", {}, self.sweeps()[0])
        self.assertEqual(decision, {"action": "maintain", "task": "sweep", "closed": 5})
        self.gh.dispatches.clear()
        self.maintain({"action": "maintain", "task": "parents", "issue": 5}, make_config())
        self.assertEqual(self.sweeps(), [])

    def test_only_events_that_can_change_the_scope_start_a_sweep(self):
        events = [
            ({"event": "opened"}, pilot(), 1),
            ({"event": "milestoned"}, pilot(), 1),
            ({"event": "milestone_closed"}, pilot(), 1),
            ({"event": "labeled", "label": "bug"}, pilot(), 0),
            ({"event": "milestoned"}, pilot(scope="label", label="go"), 0),
            ({"event": "labeled", "label": "bug"}, pilot(scope="label", label="go"), 0),
            ({"event": "labeled", "label": "go"}, pilot(scope="label", label="go"), 1),
        ]
        for event, cfg, sweeps in events:
            self.gh.dispatches.clear()
            self.maintain({"action": "maintain", "task": "autopilot", **event}, cfg)
            self.assertEqual(len(self.sweeps()), sweeps, event)

    def test_the_schedule_starts_a_sweep(self):
        self.maintain({"action": "maintain", "task": "schedule"})
        self.assertEqual(len(self.sweeps()), 1)

    def test_a_sweep_starts_commands_that_route_accepts(self):
        self.issue(5)
        self.maintain({"action": "maintain", "task": "sweep"})
        [inputs] = [d["inputs"] for d in self.gh.dispatches]
        decision = route(self.gh, "workflow_dispatch", {"sender": {"login": "bot"}}, inputs)
        self.assertEqual((decision["action"], decision["autopilot"]), ("plan", True))
        self.assertFalse(decision["auto"])


class ReportTests(AutopilotCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "acme/app", "GITHUB_RUN_ID": "7"})
        env.start()
        self.addCleanup(env.stop)

    def report_plan(self, cfg, output):
        artifacts = Path(self.tmp.name)
        (artifacts / "nexkit-agent").mkdir(exist_ok=True)
        result = {"status": "done", "output": output, "tokens": {"input": 1, "output": 1}}
        (artifacts / "nexkit-agent" / "result.json").write_text(json.dumps(result))
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
        return report(
            self.gh, decision, cfg, needs, artifacts, workflow_ref=WORKFLOW, default_branch="main"
        )

    def test_a_plan_that_needs_nobody_leads_to_go(self):
        self.issue(5)
        self.report_plan(self.cfg, {"questions": [], "too_large": False})
        [inputs] = [d["inputs"] for d in self.gh.dispatches]
        self.assertEqual(inputs, autopilot.SWEEP)
        [run] = self.gh.run_comments(5)
        self.assertTrue(run["body"].startswith("### NexKit `/nexkit plan` (autopilot)"))
        self.gh.dispatches.clear()
        self.sweep()
        self.assertEqual(self.started(), [("go", 5)])

    def test_without_autopilot_report_starts_nothing(self):
        self.issue(5)
        self.report_plan(make_config(), {"questions": [], "too_large": False})
        self.assertEqual(self.gh.dispatches, [])

    def test_a_plan_with_questions_stops_and_mentions_the_people_to_notify(self):
        self.issue(5)
        cfg = make_config(autopilot={"enabled": True}, notify=["olivia"])
        self.report_plan(cfg, {"questions": ["Which port?"], "too_large": False})
        self.assertTrue(self.gh.comments_matching(5, "@olivia a person is needed"))
        self.gh.dispatches.clear()
        self.sweep(cfg)
        self.assertEqual(self.started(), [])


class StatusTests(AutopilotCase):
    def test_status_shows_what_the_next_sweep_does(self):
        from nexkit.status import collect, render

        self.issue(4)
        self.issue(5, user="bob")
        data = collect(self.gh, make_config(), workers=1)
        self.assertNotIn("autopilot", data)
        data = collect(self.gh, pilot(max_parallel=2), workers=1)
        self.assertEqual(data["autopilot"]["scope"], "milestone M1")
        self.assertEqual(
            [(i["number"], i["state"], i.get("command")) for i in data["autopilot"]["issues"]],
            [(4, "start", "plan"), (5, "waits", None)],
        )
        text = render(data)
        self.assertIn("Autopilot: milestone M1, at most 2 at once", text)
        self.assertIn("start /nexkit plan", text)
        self.assertEqual(self.gh.dispatches, [])  # status changes nothing
