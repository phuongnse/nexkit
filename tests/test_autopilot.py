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
# The annotation the runner adds to every job whose step fails.
GENERIC_ANNOTATION = {
    "path": ".github",
    "start_line": 1,
    "annotation_level": "failure",
    "title": "",
    "message": "Process completed with exit code 1.",
}


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

    def report_plan(self, cfg, output):
        """`report` after a plan run with this output."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        artifacts = Path(tmp.name)
        (artifacts / "nexkit-agent").mkdir()
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

    def test_issues_without_a_milestone_are_taken_only_with_the_null_scope(self):
        self.issue(3, milestone=None)
        self.gh.add_milestone(1)
        self.sweep()
        self.assertEqual(self.started(), [])
        self.sweep(pilot(scope=None))
        self.assertEqual(self.started(), [("plan", 3)])

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

    def test_the_null_scope_takes_every_open_issue_by_milestone_then_age(self):
        self.gh.add_milestone(1)
        self.gh.add_milestone(2, due_on="2026-11-01T00:00:00Z")
        self.issue(3, milestone=None)
        self.issue(4, milestone=1)
        self.issue(5, milestone=2)
        self.issue(6, milestone=1)
        self.issue(7, milestone=2)
        self.issue(8, milestone=9)  # a closed milestone: with those without one
        self.gh.blockers[7] = [4]  # in another milestone, and in scope
        cfg = pilot(scope=None)
        where, steps = autopilot.preview(self.gh, cfg)
        self.assertEqual(where["name"], "every open issue")
        self.assertEqual([s["number"] for s in steps], [5, 7, 4, 6, 3, 8])
        self.assertEqual(
            (steps[1]["state"], steps[1]["reason"], steps[1]["person"]),
            ("waits", "waits for #4", False),
        )
        # Within max_parallel, the earlier milestone's work goes first.
        _, steps = autopilot.preview(self.gh, pilot(scope=None, max_parallel=2))
        self.assertEqual([s["number"] for s in steps if s["state"] == "start"], [5, 4])
        self.sweep(cfg)
        self.assertEqual(
            self.started(), [("plan", 5), ("plan", 4), ("plan", 6), ("plan", 3), ("plan", 8)]
        )
        self.gh.dispatches.clear()
        self.gh.close_issue(4)
        self.sweep(cfg, closed=4)
        self.assertEqual(self.started(), [("plan", 7)])

    def test_the_null_scope_says_once_that_a_milestone_is_done(self):
        self.gh.add_milestone(1)
        self.gh.add_milestone(2)
        self.issue(4, milestone=1)
        self.issue(5, milestone=1)
        self.issue(6, milestone=2)
        cfg = pilot(scope=None)
        cfg["notify"] = ["olivia"]
        self.gh.close_issue(4)
        self.assertFalse(any("finished" in line for line in self.sweep(cfg, closed=4)))
        self.assertEqual(self.gh.comments_matching(4, autopilot.DONE), [])
        self.gh.close_issue(5)
        lines = self.sweep(cfg, closed=5)
        [note] = self.gh.comments_matching(5, autopilot.DONE)
        self.assertIn("@olivia NexKit autopilot finished milestone M1", note["body"])
        self.assertNotIn("Close the milestone", note["body"])
        self.assertIn("Autopilot finished milestone M1.", lines)
        self.sweep(cfg, closed=5)
        self.assertEqual(len(self.gh.comments_matching(5, autopilot.DONE)), 1)
        # Milestone M2 goes on, and each issue is started once.
        self.assertEqual(self.started(), [("plan", 5), ("plan", 6)])
        # An issue without a milestone ends no milestone.
        self.issue(7, milestone=None, state="closed")
        self.sweep(cfg, closed=7)
        self.assertEqual(self.gh.comments_matching(7, autopilot.DONE), [])

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

    def test_a_command_that_never_ran_starts_once_more_then_waits(self):
        self.issue(5)
        self.sweep()
        later = datetime.now(UTC) + timedelta(hours=7)
        _, [step] = autopilot.preview(self.gh, self.cfg)
        self.assertEqual(step["state"], "active")
        step = autopilot.assess(self.gh, self.cfg, self.gh.issues[5], {}, now=later)
        self.assertEqual((step["state"], step["command"]), ("start", "plan"))
        self.assertIn("did not run, so autopilot starts it once more", step["reason"])
        autopilot.start(self.gh, step, "nexkit.yml", "main", now=later)
        self.assertEqual(self.started(), [("plan", 5), ("plan", 5)])
        # It does not run again either: a person decides.
        much_later = later + timedelta(hours=7)
        step = autopilot.assess(self.gh, self.cfg, self.gh.issues[5], {}, now=much_later)
        self.assertEqual((step["state"], step["person"]), ("waits", True))
        self.assertIn("did not run, again", step["reason"])
        # A later start of another command can be started once more again.
        self.run_comment(5, "plan", status="running")
        self.gh.issue_comments[5].pop()
        self.planned(5)
        self.sweep()
        record = autopilot.find_record(self.gh.comments(5))[1]
        self.assertEqual((record["command"], record.get("retried")), ("go", None))

    def test_a_note_of_a_command_that_never_ran_is_kept(self):
        cfg = pilot(answer="recommended")
        self.issue(5)
        self.planned(5)
        self.run_comment(5, "go", status="failure", outcome="agent_blocked", questions=[QUESTION])
        self.sweep(cfg)
        later = datetime.now(UTC) + timedelta(hours=7)
        step = autopilot.assess(self.gh, cfg, self.gh.issues[5], {}, now=later)
        self.assertIn("Which port? Answer: 8080", step["note"])
        self.assertNotIn("answers", step)  # posted once already

    def test_a_run_that_ended_without_a_report_starts_once_more(self):
        self.issue(5)
        self.planned(5)
        self.run_comment(5, "go", status="running", autopilot=True)
        self.gh.runs[self.runs] = {"status": "in_progress"}
        _, [step] = autopilot.preview(self.gh, self.cfg)
        self.assertEqual(step["state"], "active")
        self.gh.runs[self.runs] = {"status": "completed", "conclusion": "failure"}
        self.sweep()
        self.assertEqual(self.started(), [("go", 5)])
        [run] = [c["run"] for c in self.gh.run_comments(5) if c["run"]["command"] == "go"]
        self.assertEqual((run["status"], run["attention"]), ("failure", autopilot.REPORTLESS))
        # Its start ends without a report too: now a person decides.
        self.run_comment(5, "go", status="running", autopilot=True)
        self.gh.runs[self.runs] = {"status": "completed", "conclusion": "cancelled"}
        _, [step] = autopilot.preview(self.gh, self.cfg)
        self.assertEqual((step["state"], step["reason"]), ("waits", autopilot.REPORTLESS))
        # A person's command is never started again by autopilot.
        self.issue(6)
        self.planned(6)
        self.run_comment(6, "go", status="running")
        self.gh.runs[self.runs] = {"status": "completed"}
        steps = {s["number"]: s for s in autopilot.preview(self.gh, self.cfg)[1]}
        self.assertEqual((steps[6]["state"], steps[6]["reason"]), ("waits", autopilot.REPORTLESS))

    def test_a_go_whose_review_continues_on_its_pull_request_follows_the_pull_request(self):
        self.issue(5)
        self.planned(5)
        self.run_comment(5, "go", status="continued", outcome="retry")
        self.gh.add_pull(6, 5)
        row = {"round": 2, "trigger": "review (retry)", "url": "u", "status": "success"}
        self.gh.comment(6, render_run({**row, "outcome": "needs_human", "attention": "x"}))
        _, [step] = autopilot.preview(self.gh, self.cfg)
        self.assertEqual((step["state"], step["reason"]), ("waits", "x"))

    def test_a_chained_round_whose_next_round_never_ran_waits(self):
        self.issue(5)
        self.planned(5)
        self.run_comment(5, "go", outcome="ready")
        self.gh.add_pull(6, 5)
        row = {"round": 1, "trigger": "implement", "url": "u", "status": "success"}
        comment = self.gh.comment(6, render_run({**row, "outcome": "auto_fix"}))
        comment["updated_at"] = "2026-01-01T00:00:00Z"
        soon = datetime(2026, 1, 1, 1, tzinfo=UTC)
        step = autopilot.assess(self.gh, self.cfg, self.gh.issues[5], {5: 6}, now=soon)
        self.assertEqual(step["state"], "active")
        late = datetime(2026, 1, 1, 7, tzinfo=UTC)
        step = autopilot.assess(self.gh, self.cfg, self.gh.issues[5], {5: 6}, now=late)
        self.assertEqual((step["state"], step["person"]), ("waits", True))

    def test_a_retry_of_another_command_still_lets_a_stalled_round_start_once_more(self):
        self.issue(5)
        self.planned(5)
        self.run_comment(5, "go", outcome="ready")
        self.gh.add_pull(6, 5)
        record = {"command": "go", "target": 5, "retried": True, "basis": [0, 0], "at": "x"}
        self.gh.comment(5, "Started.\n\n" + autopilot.record_marker(record))
        row = {
            "round": 2,
            "trigger": "fix (auto)",
            "url": "https://github.com/acme/app/actions/runs/88",
        }
        self.gh.comment(6, render_run({**row, "status": "running", "head": "b" * 40}))
        self.gh.runs[88] = {"status": "completed"}
        _, [step] = autopilot.preview(self.gh, self.cfg)
        self.assertEqual((step["state"], step["command"]), ("start", "fix"))

    def test_a_round_that_ended_without_a_report_gets_its_statuses_back(self):
        self.issue(5)
        self.planned(5)
        self.run_comment(5, "go", outcome="ready")
        self.gh.add_pull(6, 5)
        row = {
            "round": 2,
            "trigger": "fix (auto)",
            "url": "https://github.com/acme/app/actions/runs/77",
        }
        self.gh.comment(6, render_run({**row, "status": "running", "head": "b" * 40}))
        self.gh.runs[77] = {"status": "completed"}
        self.sweep()
        self.assertEqual(self.started(), [("fix", 6)])
        errors = {(s["sha"], s["context"], s["state"]) for s in self.gh.statuses}
        self.assertIn(("b" * 40, "nexkit/review", "error"), errors)

    def test_a_pause_that_nexkit_does_not_resume_waits_for_a_person(self):
        self.issue(4)
        self.run_comment(4, "plan", status="paused", attention="run the command after the reset")
        self.issue(5)
        resume = {"command": "plan", "number": 5, "at": "2026-01-01T05:00:00Z"}
        self.run_comment(5, "plan", status="paused", resume=resume)
        steps = {s["number"]: s for s in autopilot.preview(self.gh, self.cfg)[1]}
        self.assertEqual(
            (steps[4]["state"], steps[4]["reason"]), ("waits", "run the command after the reset")
        )
        # The fake's comments are from 2026-01-01: within the window, then outside it.
        soon = datetime(2026, 1, 3, tzinfo=UTC)
        step = autopilot.assess(self.gh, self.cfg, self.gh.issues[5], {}, now=soon)
        self.assertEqual(step["state"], "active")
        late = datetime(2026, 1, 20, tzinfo=UTC)
        step = autopilot.assess(self.gh, self.cfg, self.gh.issues[5], {}, now=late)
        self.assertEqual(step["state"], "waits")
        self.assertIn("longer than NexKit resumes runs", step["reason"])

    def test_a_continued_run_whose_new_run_never_started_waits(self):
        self.issue(5)
        comment = self.run_comment(5, "plan", status="continued")
        comment["updated_at"] = "2026-01-01T00:00:00Z"
        soon = datetime(2026, 1, 1, 2, tzinfo=UTC)
        self.assertEqual(
            autopilot.assess(self.gh, self.cfg, self.gh.issues[5], {}, now=soon)["state"],
            "active",
        )
        late = datetime(2026, 1, 1, 7, tzinfo=UTC)
        step = autopilot.assess(self.gh, self.cfg, self.gh.issues[5], {}, now=late)
        self.assertEqual((step["state"], step["person"]), ("waits", True))

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

    def test_sub_issues_wait_only_for_the_issues_that_block_them(self):
        for number in (2, 3, 4, 5):
            self.issue(number)
        self.gh.parents.update({3: 2, 4: 2, 5: 2})
        self.gh.blockers[5] = [3]
        _, steps = autopilot.preview(self.gh, self.cfg)
        self.assertEqual([s["state"] for s in steps], ["waits", "start", "start", "waits"])
        self.assertIn("a parent issue", steps[0]["reason"])
        self.assertEqual(steps[3]["reason"], "waits for #3")


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
            # With null every open issue is in scope, whatever its milestone.
            ({"event": "opened"}, pilot(scope=None), 1),
            ({"event": "reopened"}, pilot(scope=None), 1),
            ({"event": "milestoned"}, pilot(scope=None), 0),
            ({"event": "milestone_closed"}, pilot(scope=None), 0),
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
        self.issue(6, milestone=None)
        data = collect(self.gh, pilot(scope=None), workers=1)
        self.assertEqual(data["autopilot"]["scope"], "every open issue")
        self.assertEqual([i["number"] for i in data["autopilot"]["issues"]], [4, 5, 6])
        self.assertIn("Autopilot: every open issue", render(data))
        self.assertEqual(self.gh.dispatches, [])  # status changes nothing


# As an agent returns it: the recommended option by its number, from 1.
QUESTION = {"question": "Which port?", "options": ["8080", "9090"], "recommended": 1}
# As NexKit records and shows it: by its text.
RECORDED = {**QUESTION, "recommended": "8080"}


def plan_output(**kw):
    return {
        "summary": "Add it.",
        "changes": ["It is added."],
        "decisions": [],
        "risks": [],
        "acceptance_criteria": ["It works."],
        "implementation_notes": ["Edit app.py."],
        "questions": [],
        "too_large": False,
        "split": [],
        "revision": {"started_over": False, "changes": []},
        **kw,
    }


class AnswerAndSplitCase(AutopilotCase):
    def setUp(self):
        super().setUp()
        self.cfg = pilot(answer="recommended")

    def posted_plan(self, number, **output):
        """A plan run and its comment, as `publish` writes it."""
        from nexkit.publish import render_plan

        self.run_comment(number, "plan", outcome="planned")
        return self.gh.comment(number, render_plan(plan_output(**output)))


class AnswerTests(AnswerAndSplitCase):
    def test_answers_recommended_questions_and_plans_again(self):
        from nexkit.context import gather

        self.issue(5)
        self.posted_plan(5, questions=[QUESTION])
        self.sweep()
        self.assertEqual(self.started(), [("plan", 5)])
        [answered] = self.gh.comments_matching(5, "answers for the owner")
        self.assertIn("1. Which port?\n   **8080**", answered["body"])
        # The next plan reads the answers as the owner's discussion since the plan.
        decision = {"action": "plan", "issue": 5, "pr": None, "note": ""}
        context = gather(self.gh, decision)
        self.assertIn("NexKit autopilot for the owner", context["since_plan"])
        self.assertIn("**8080**", context["since_plan"])
        self.assertNotIn("nexkit:answers", context["since_plan"])
        # The answer comment is not a collaborator's change: the sweep does not re-plan twice.
        self.sweep()
        self.assertEqual(self.started(), [("plan", 5)])

    def test_another_bots_answers_are_not_the_owners(self):
        from nexkit.context import gather

        self.issue(5)
        comment = self.gh.comment(5, "x\n\n" + autopilot.answers_marker({"answers": []}))
        comment["user"] = {"login": "evil[bot]", "type": "Bot"}
        context = gather(self.gh, {"action": "plan", "issue": 5, "pr": None, "note": ""})
        self.assertNotIn("for the owner", context["discussion"])

    def test_waits_for_a_person_without_a_recommendation_or_without_answer(self):
        self.issue(4)
        self.posted_plan(4, questions=[{**QUESTION, "recommended": 0}])
        self.issue(5)
        self.posted_plan(5, questions=[QUESTION])
        _, steps = autopilot.preview(self.gh, self.cfg)
        self.assertEqual([s["state"] for s in steps], ["waits", "start"])
        _, steps = autopilot.preview(self.gh, pilot())  # answer: "never"
        self.assertEqual([s["state"] for s in steps], ["waits", "waits"])
        # A number out of range, a boolean or text that is not an option is no recommendation.
        for chosen in (3, -1, True, "80"):
            self.assertIsNone(autopilot.answers(self.cfg, [{**QUESTION, "recommended": chosen}]))
        # The second option, by number; a record that holds the text still counts.
        found = autopilot.answers(self.cfg, [{**QUESTION, "recommended": 2}])
        self.assertEqual(found, [{"question": "Which port?", "answer": "9090"}])
        self.assertEqual(autopilot.answers(self.cfg, [RECORDED])[0]["answer"], "8080")

    def test_answers_a_blocked_go_and_a_blocked_fix_round(self):
        self.issue(4)
        self.posted_plan(4)
        self.run_comment(
            4,
            "go",
            status="failure",
            outcome="agent_blocked",
            attention="blocked",
            questions=[QUESTION],
        )
        self.issue(5)
        self.posted_plan(5)
        self.run_comment(5, "go", outcome="ready")
        self.gh.add_pull(6, 5)
        round_ = {
            "round": 2,
            "trigger": "fix",
            "status": "failure",
            "url": "u",
            "outcome": "agent_blocked",
            "questions": [QUESTION],
        }
        self.gh.comment(6, render_run(round_))
        self.sweep()
        self.assertEqual(self.started(), [("go", 4), ("fix", 6)])
        notes = [d["inputs"]["note"] for d in self.gh.dispatches]
        self.assertTrue(all("Which port? Answer: 8080" in note for note in notes))
        self.assertTrue(self.gh.comments_matching(6, "answers for the owner"))
        self.assertTrue(self.gh.comments_matching(4, "answers for the owner"))
        self.sweep()
        self.assertEqual(len(self.started()), 2)

    def test_an_answer_that_only_closes_the_issue_closes_it_without_a_round(self):
        finding = "8b9cb87 fixed it; the last 20 runs passed."
        self.issue(4)
        self.posted_plan(4)
        self.blocked(4, no_change=finding)
        self.sweep()
        self.assertEqual(self.started(), [])
        self.assertEqual(self.gh.issues[4]["state"], "closed")
        [comment] = self.gh.comments_matching(4, "NexKit autopilot closes this issue")
        self.assertIn("[the last round](https://github.com/acme/app/actions/runs/", comment["body"])
        self.assertTrue(comment["body"].endswith(finding))
        # A sweep follows, for the issues it blocked and the end of the scope.
        self.assertEqual(self.gh.dispatches[-1]["inputs"]["command"], "autopilot")
        # Without `autopilot.answer: "recommended"` a person decides, as for any question.
        self.issue(5)
        self.posted_plan(5)
        self.blocked(5, no_change=finding)
        _, steps = autopilot.preview(self.gh, pilot())
        self.assertEqual([(s["number"], s["state"]) for s in steps], [(5, "waits")])
        # With a second question, closing may not be all the agent recommends: it is
        # answered as any blocked round.
        self.run_comment(
            5,
            "go",
            status="failure",
            outcome="agent_blocked",
            questions=[QUESTION, {**QUESTION, "question": "Q2?"}],
            no_change=finding,
        )
        _, steps = autopilot.preview(self.gh, self.cfg)
        self.assertEqual([(s["number"], s["state"]) for s in steps], [(5, "start")])

    def blocked(self, number, **fields):
        return self.run_comment(
            number,
            "go",
            status="failure",
            outcome="agent_blocked",
            attention="the agent is blocked and needs a decision",
            questions=[QUESTION],
            **fields,
        )

    def test_a_round_that_blocks_again_after_autopilots_answer_waits_for_a_person(self):
        from nexkit.status import collect

        self.issue(4)
        self.posted_plan(4)
        self.blocked(4)
        self.sweep()
        self.assertEqual(self.started(), [("go", 4)])
        # The go that autopilot's answers started blocks again, and nobody did anything.
        self.blocked(4)
        decision = {"action": "implement", "issue": 4, "pr": None}
        agent = {"status": "blocked", "output": {"questions": [QUESTION]}}
        self.assertFalse(autopilot.carries_on(self.gh, self.cfg, decision, "agent_blocked", agent))
        self.sweep()
        self.assertEqual(self.started(), [("go", 4)])
        self.assertEqual(len(self.gh.comments_matching(4, "answers for the owner")), 1)
        _, [step] = autopilot.preview(self.gh, self.cfg)
        self.assertEqual((step["state"], step["reason"]), ("waits", autopilot.REPEATED))
        data = collect(self.gh, self.cfg, workers=1)
        self.assertIn(4, [item["number"] for item in data["needs_person"]])
        # A person's comment since the answers lets autopilot answer again.
        self.gh.human_comment(4, "I attached the files to the issue.")
        self.assertTrue(autopilot.carries_on(self.gh, self.cfg, decision, "agent_blocked", agent))
        self.sweep()
        self.assertEqual(self.started(), [("go", 4), ("go", 4)])

    def test_a_fix_round_that_blocks_again_after_autopilots_answer_waits(self):
        self.issue(5)
        self.posted_plan(5)
        self.run_comment(5, "go", outcome="ready")
        self.gh.add_pull(6, 5)
        round_ = {"trigger": "fix", "status": "failure", "outcome": "agent_blocked"}
        self.gh.comment(6, render_run({**round_, "round": 2, "url": "u2", "questions": [QUESTION]}))
        self.sweep()
        self.assertEqual(self.started(), [("fix", 6)])
        self.gh.comment(6, render_run({**round_, "round": 3, "url": "u3", "questions": [QUESTION]}))
        self.sweep()
        self.assertEqual(self.started(), [("fix", 6)])
        # A round that ran in between, such as one after new commits, is not the answered one.
        self.gh.comment(6, render_run({**round_, "round": 4, "url": "u4", "questions": [QUESTION]}))
        self.assertFalse(autopilot.asked_again(self.gh.comments(6)))
        # A comment by a person on the issue counts as well.
        self.gh.issue_comments[6].pop()
        self.gh.human_comment(5, "Use the other port.")
        self.sweep()
        self.assertEqual(self.started(), [("fix", 6), ("fix", 6)])

    def test_a_plan_that_asks_again_after_autopilots_answer_waits_for_a_person(self):
        cfg = make_config(autopilot={"enabled": True, "answer": "recommended"}, notify=["olivia"])
        self.issue(5)
        self.posted_plan(5, questions=[QUESTION])
        self.sweep(cfg)
        self.assertEqual(self.started(), [("plan", 5)])
        again = {**QUESTION, "question": "Which host?", "options": ["a", "b"]}
        self.run_comment(5, "plan", status="running")  # as `route` posts it
        result = self.report_plan(cfg, plan_output(questions=[again]))
        self.assertEqual(result["attention"], "the plan has questions")
        self.assertTrue(self.gh.comments_matching(5, "@olivia a person is needed"))
        self.sweep(cfg)
        self.assertEqual(self.started(), [("plan", 5)])
        # The same holds for a plan with its record, as `publish` posts it.
        self.gh.issues[5]["state"] = "closed"
        self.issue(6)
        self.posted_plan(6, questions=[QUESTION])
        self.sweep(cfg)
        self.posted_plan(6, questions=[again])
        _, [step] = autopilot.preview(self.gh, cfg)
        self.assertEqual((step["state"], step["reason"]), ("waits", autopilot.REPEATED))

    def test_a_retried_round_between_the_answer_and_the_next_block_changes_nothing(self):
        self.issue(4)
        self.posted_plan(4)
        self.blocked(4)
        self.sweep()
        self.run_comment(4, "go", status="continued", outcome="retry")
        self.blocked(4)
        self.sweep()
        self.assertEqual(self.started(), [("go", 4)])
        _, [step] = autopilot.preview(self.gh, self.cfg)
        self.assertEqual(step["reason"], autopilot.REPEATED)

    def test_prompts_recommend_only_what_nexkit_can_carry_out(self):
        from nexkit.agent import _template

        for name in ("plan", "implement", "fix"):
            text = " ".join(_template(name).split())
            self.assertIn("Recommend only an option that a change to the plan or the code", text)
            self.assertIn("needs a person to act outside the repository", text)
        diagnosis = _template("fix_diagnosis")
        self.assertIn("add logging or narrow the test in the working tree yourself", diagnosis)
        self.assertNotIn("such as more logging", diagnosis)

    def test_report_mentions_nobody_when_autopilot_answers(self):
        cfg = make_config(autopilot={"enabled": True, "answer": "recommended"}, notify=["olivia"])
        self.issue(5)
        result = self.report_plan(cfg, plan_output(questions=[QUESTION]))
        self.assertIsNone(result.get("attention"))
        self.assertIn("NexKit autopilot carries on from here.", result["summary"])
        self.assertFalse(self.gh.comments_matching(5, "@olivia"))
        self.report_plan(cfg, plan_output(questions=[{**QUESTION, "recommended": 0}]))
        self.assertTrue(self.gh.comments_matching(5, "@olivia a person is needed"))
        # Outside the scope, autopilot answers nothing, so a person is needed.
        self.gh.issue_comments[5].clear()
        self.gh.issues[5]["milestone"] = None
        self.report_plan(cfg, plan_output(questions=[QUESTION]))
        self.assertTrue(self.gh.comments_matching(5, "@olivia a person is needed"))


class SplitTests(AnswerAndSplitCase):
    PARTS = [
        {"title": "First part", "body": "Do A.", "depends_on": []},
        {"title": "Second part", "body": "Do B.", "depends_on": [1]},
    ]

    def test_a_plan_that_is_too_large_becomes_sub_issues_and_the_first_is_planned(self):
        self.issue(5, labels=["area"])
        self.posted_plan(5, too_large=True, split=self.PARTS)
        lines = self.sweep(pilot())  # splits without autopilot.answer
        self.assertIn("Split #5 into #6, #7.", lines)
        self.assertEqual(
            [self.gh.issues[n]["title"] for n in (6, 7)], ["First part", "Second part"]
        )
        for number in (6, 7):
            issue = self.gh.issues[number]
            self.assertEqual(self.gh.parents[number], 5)
            self.assertEqual(issue["milestone"], {"number": 1})
            self.assertEqual(issue["labels"], [{"name": "area"}])
            self.assertTrue(issue["body"].startswith("<!-- nexkit:part 5 -->"))
        self.assertEqual(self.started(), [("plan", 6)])
        self.assertEqual(self.gh.blockers[7], [6])
        self.assertTrue(self.gh.comments_matching(5, "split this issue as its plan suggests"))
        self.sweep(pilot())
        self.assertEqual(self.started(), [("plan", 6)])

    def test_parts_that_do_not_need_each_other_are_planned_at_once(self):
        self.issue(5)
        parts = [
            {"title": "One", "body": "A.", "depends_on": []},
            {"title": "Two", "body": "B.", "depends_on": [2, 3, 0, True, "1"]},  # all invalid
            {"title": "Three", "body": "C.", "depends_on": [1, 1]},
        ]
        self.posted_plan(5, too_large=True, split=parts)
        self.sweep()
        self.assertEqual(self.started(), [("plan", 6), ("plan", 7)])
        self.assertEqual((self.gh.blockers[7], self.gh.blockers[8]), ([], [6]))
        [plan] = self.gh.comments_matching(5, "### Suggested split")
        self.assertIn("1. **One**: A.\n2. **Two**: B.\n3. **Three** (after 1): C.", plan["body"])

    def test_a_plan_record_without_dependencies_keeps_the_parts_in_order(self):
        self.issue(5)
        self.run_comment(5, "plan", outcome="planned")
        split = [{"title": t, "body": "x"} for t in ("One", "Two", "Three")]
        from nexkit.state import PLAN_DATA_PREFIX, _encode

        data = {"questions": [], "too_large": True, "split": split}
        self.gh.comment(5, f"{PLAN_MARKER}\n## Plan\n\n{_encode(PLAN_DATA_PREFIX, data)}")
        self.sweep()
        self.assertEqual(self.started(), [("plan", 6)])
        self.assertEqual((self.gh.blockers[7], self.gh.blockers[8]), ([6], [7]))

    def test_only_parts_of_a_trusted_parent_are_trusted(self):
        self.issue(5, user="bob")
        self.issue(6, user="github-actions[bot]", body="<!-- nexkit:part 5 -->\nDo A.")
        self.gh.parents[6] = 5
        self.issue(7, user="github-actions[bot]", body="<!-- nexkit:part 5 -->\nDo B.")
        self.assertFalse(autopilot.trusted(self.gh, self.gh.issues[6]))  # bob cannot write
        self.assertFalse(autopilot.trusted(self.gh, self.gh.issues[7]))  # not a sub-issue
        self.gh.issues[5]["user"] = {"login": "alice"}
        self.assertTrue(autopilot.trusted(self.gh, self.gh.issues[6]))

    def test_an_agent_that_ran_out_of_time_leads_to_one_split_plan(self):
        self.issue(5)
        self.posted_plan(5)
        self.run_comment(
            5,
            "go",
            status="failure",
            outcome="agent_error",
            timed_out=True,
            attention="the run stopped on an error",
        )
        self.sweep()
        self.assertEqual(self.started(), [("plan", 5)])
        self.assertIn(
            "did not finish within its time limit", self.gh.dispatches[0]["inputs"]["note"]
        )
        # The new plan is not too large, and go runs out of time again: a person decides.
        self.posted_plan(5)
        self.sweep()
        self.run_comment(
            5,
            "go",
            status="failure",
            outcome="agent_error",
            timed_out=True,
            attention="the run stopped on an error",
        )
        self.sweep()
        self.assertEqual(self.started(), [("plan", 5), ("go", 5)])
        _, [step] = autopilot.preview(self.gh, self.cfg)
        self.assertEqual((step["state"], step["reason"]), ("waits", "the run stopped on an error"))

    def test_a_fix_round_out_of_time_splits_and_closes_the_pull_request(self):
        self.issue(5)
        self.posted_plan(5)
        self.run_comment(5, "go", outcome="ready")
        self.gh.add_pull(9, 5)
        round_ = {
            "round": 2,
            "trigger": "fix",
            "status": "failure",
            "url": "u",
            "outcome": "agent_error",
            "timed_out": True,
        }
        self.gh.comment(9, render_run(round_))
        self.sweep()
        self.assertEqual(self.started(), [("plan", 5)])
        self.posted_plan(5, too_large=True, split=self.PARTS)
        self.sweep()
        self.assertEqual(self.gh.pulls[9]["state"], "closed")
        self.assertTrue(self.gh.comments_matching(9, "Superseded by #10, #11"))
        self.assertEqual(self.started(), [("plan", 5), ("plan", 10)])

    def test_a_too_large_plan_that_a_person_implemented_anyway_is_not_split(self):
        self.issue(5)
        self.posted_plan(5, too_large=True, split=self.PARTS)
        self.run_comment(5, "go", outcome="ready")  # a person commented /nexkit go
        self.gh.add_pull(9, 5)
        self.sweep()
        self.assertEqual((self.started(), self.gh.pulls[9]["state"]), ([], "open"))

    def timed_out(self, number):
        self.run_comment(
            number,
            "go",
            status="failure",
            outcome="agent_error",
            timed_out=True,
            attention="the run stopped on an error",
        )

    def part(self, number, parent):
        """A part that autopilot created, as a sub-issue of `parent`."""
        body = f"<!-- nexkit:part {parent} -->\nDo it."
        self.issue(number, user="github-actions[bot]", body=body)
        self.gh.parents[number] = parent

    def test_a_part_of_an_issue_that_asked_for_a_split_waits_after_its_own_time_out(self):
        from nexkit.status import collect, render

        self.issue(5)
        self.posted_plan(5)
        self.timed_out(5)
        self.sweep()
        self.assertEqual(self.gh.dispatches[0]["inputs"]["note"], autopilot.SPLIT_NOTE)
        self.posted_plan(5, too_large=True, split=self.PARTS)
        self.sweep()
        self.assertEqual(self.started(), [("plan", 5), ("plan", 6)])
        # The part's own agent runs out of time: it inherits #5's split, so a person decides.
        self.posted_plan(6)
        self.timed_out(6)
        decision = {"action": "implement", "issue": 6}
        agent = {"timed_out": True}
        self.assertFalse(autopilot.carries_on(self.gh, self.cfg, decision, "agent_error", agent))
        self.sweep()
        self.assertEqual(self.started(), [("plan", 5), ("plan", 6)])
        reason = "a part of a split ran out of time again, after #5 asked for a split"
        steps = {s["number"]: s for s in autopilot.preview(self.gh, self.cfg)[1]}
        self.assertEqual((steps[6]["state"], steps[6]["reason"]), ("waits", reason))
        self.assertIn(reason, render(collect(self.gh, self.cfg, workers=1)))
        # A part of that part, two levels below #5, inherits it too.
        self.part(8, 6)
        self.posted_plan(8)
        self.timed_out(8)
        self.assertEqual(autopilot.split_refused(self.gh, self.gh.issues[8]), reason)

    def test_parts_titled_like_their_parent_are_not_created_and_the_plan_is_asked_again(self):
        self.assertEqual(
            autopilot.repeated_titles(
                {"title": " Issue 5. "},
                [{"title": "issue 5"}, {"title": "ISSUE 5: the rest!"}, {"title": "Add it"}],
            ),
            ["issue 5", "ISSUE 5: the rest!"],
        )
        self.issue(5)
        parts = [
            {"title": "Fixtures for issue 5", "body": "Do A.", "depends_on": []},
            {"title": "Issue 5.", "body": "Do B.", "depends_on": [1]},
        ]
        self.posted_plan(5, too_large=True, split=parts)
        decision = {"action": "plan", "issue": 5}
        agent = {"output": {"too_large": True, "split": parts}}
        self.assertTrue(autopilot.carries_on(self.gh, self.cfg, decision, "planned", agent))
        self.sweep()
        self.assertEqual(self.started(), [("plan", 5)])
        self.assertEqual(sorted(self.gh.issues), [5])  # no parts created
        note = self.gh.dispatches[0]["inputs"]["note"]
        self.assertEqual(note, autopilot.TITLE_NOTE.format(titles='"Issue 5."'))
        # The new plan repeats the title again: a person decides, and nothing loops.
        self.posted_plan(5, too_large=True, split=parts)
        self.assertFalse(autopilot.carries_on(self.gh, self.cfg, decision, "planned", agent))
        self.sweep()
        self.assertEqual((self.started(), sorted(self.gh.issues)), ([("plan", 5)], [5]))
        _, [step] = autopilot.preview(self.gh, self.cfg)
        reason = "the parts of its plan are titled like the issue again"
        self.assertEqual((step["state"], step["reason"]), ("waits", reason))
        # A plan with parts titled by their own behaviour splits.
        self.posted_plan(5, too_large=True, split=self.PARTS)
        self.sweep()
        self.assertEqual(self.started(), [("plan", 5), ("plan", 6)])

    def test_a_part_at_the_depth_limit_waits_for_a_person(self):
        self.issue(5)
        self.part(6, 5)
        self.part(7, 6)
        self.part(8, 7)  # depth 3
        self.part(9, 7)  # depth 3
        self.issue(10, user="github-actions[bot]", body="<!-- nexkit:part 5 -->\nDo it.")
        self.gh.parents[10] = 7  # names another parent: not one of autopilot's parts
        depths = [
            autopilot.depth(self.gh.issues[n], autopilot.lineage(self.gh, self.gh.issues[n]))
            for n in (5, 6, 7, 8, 10)
        ]
        self.assertEqual(depths, [0, 1, 2, 3, 0])
        self.posted_plan(8, too_large=True, split=self.PARTS)
        self.posted_plan(9)
        self.timed_out(9)  # no issue above asked for a split, but #9 is at the limit
        decision = {"action": "plan", "issue": 8}
        agent = {"output": {"too_large": True, "split": self.PARTS}}
        self.assertFalse(autopilot.carries_on(self.gh, self.cfg, decision, "planned", agent))
        self.sweep()
        self.assertEqual(self.started(), [])
        self.assertEqual(sorted(self.gh.issues), [5, 6, 7, 8, 9, 10])
        steps = {s["number"]: s for s in autopilot.preview(self.gh, self.cfg)[1]}
        for number in (8, 9):
            step = steps[number]
            self.assertEqual((step["state"], step["reason"]), ("waits", autopilot.DEPTH_REASON))
        self.assertIn("3 levels deep", autopilot.DEPTH_REASON)


class WaitTests(AnswerAndSplitCase):
    """Issues that need a person wait, are listed, mention `notify` and leave their place."""

    def setUp(self):
        super().setUp()
        self.cfg = make_config(
            autopilot={"enabled": True, "answer": "recommended", "max_parallel": 1},
            conflicts={"auto_resolve": True, "max_rounds": 1},
            notify=["olivia"],
        )

    def with_pull(self, issue, pull, **run):
        self.issue(issue)
        self.posted_plan(issue)
        self.run_comment(issue, "go", outcome="ready")
        self.gh.add_pull(pull, issue)
        row = {"round": 1, "trigger": "implement", "url": f"u{pull}", "status": "success"}
        self.gh.comment(pull, render_run({**row, **run}))

    def test_a_pull_request_that_needs_a_person_waits_and_leaves_its_place(self):
        from nexkit.status import collect

        attention = "checks or review still need attention and no automatic fix rounds remain"
        self.with_pull(1, 11, outcome="needs_human", attention=attention)
        self.with_pull(2, 12, status="failure", outcome="review_failed", attention="no review")
        self.with_pull(3, 13, outcome="merge_refused", attention="GitHub refused the merge")
        self.with_pull(4, 14, outcome="ready")
        self.gh.comment(
            14,
            render_run(
                {
                    "round": 2,
                    "trigger": "fix (auto, conflicts)",
                    "url": "c",
                    "status": "success",
                    "conflicts": True,
                }
            ),
        )
        self.gh.pulls[14]["mergeable"] = False
        self.issue(5)
        _, steps = autopilot.preview(self.gh, self.cfg)
        reasons = {s["number"]: (s["state"], s["reason"]) for s in steps}
        self.assertEqual(reasons[1], ("waits", attention))
        self.assertEqual(reasons[2], ("waits", "no review"))
        self.assertEqual(reasons[3], ("waits", "GitHub refused the merge"))
        used = "pull request #14 conflicts with `main`, and its conflict rounds are used up"
        self.assertEqual(reasons[4], ("waits", used))
        # None of them takes the one place, so the ready issue starts.
        self.assertEqual(reasons[5][0], "start")
        self.sweep()
        self.assertEqual(self.started(), [("plan", 5)])
        # Each one mentions `notify` once, and `nexkit status` lists each with its reason.
        for number in (1, 2, 3, 4):
            self.assertEqual(
                len(self.gh.comments_matching(number, "@olivia a person is needed")), 1
            )
        self.sweep()
        for number in (1, 2, 3, 4):
            self.assertEqual(
                len(self.gh.comments_matching(number, "@olivia a person is needed")), 1
            )
        data = collect(self.gh, self.cfg, workers=1)
        listed = {(item["number"], item["reason"]) for item in data["needs_person"]}
        for number in (1, 2, 3, 4):
            self.assertTrue(
                {(number, reasons[number][1]), (number + 10, reasons[number][1])} & listed,
                number,
            )

    def test_every_reason_to_wait_for_a_person_is_listed_by_status(self):
        from nexkit.status import collect, render

        self.issue(1, user="bob")
        self.issue(2)
        self.posted_plan(2)
        self.run_comment(2, "go", outcome="ready")  # its pull request was closed since
        self.issue(3)
        self.gh.add_issue(9, title="Elsewhere", milestone=None)
        self.gh.blockers[3] = [9]
        self.issue(4)
        self.gh.blockers[4] = [2]  # in scope: autopilot works on it
        data = collect(self.gh, self.cfg, workers=1)
        steps = {s["number"]: s for s in data["autopilot"]["issues"]}
        needs = {item["number"]: item["reason"] for item in data["needs_person"]}
        for number in (1, 2, 3):
            self.assertEqual(
                (steps[number]["state"], needs[number]), ("waits", steps[number]["reason"])
            )
        self.assertEqual(needs[3], "waits for #9; #9 is not in scope")
        self.assertNotIn(4, needs)
        self.assertNotIn("Needs a person\n  nothing", render(data))

    def test_a_fix_round_out_of_time_is_carried_on_as_report_promised(self):
        decision = {"action": "fix", "issue": 5, "pr": 9}
        agent = {"status": "error", "timed_out": True}
        self.with_pull(5, 9)
        self.assertTrue(autopilot.carries_on(self.gh, self.cfg, decision, "agent_error", agent))
        round_ = {
            "trigger": "fix",
            "status": "failure",
            "outcome": "agent_error",
            "timed_out": True,
        }
        self.gh.comment(9, render_run({**round_, "round": 2, "url": "f2"}))
        self.sweep()
        self.assertEqual(self.started(), [("plan", 5)])
        # The plan is not too large, so a fix round follows it on the pull request.
        self.posted_plan(5)
        self.sweep()
        self.assertEqual(self.started(), [("plan", 5), ("fix", 9)])
        self.assertIn("the plan was revised since", self.gh.dispatches[-1]["inputs"]["note"])
        # That one runs out of time too: now a person decides, and `notify` hears it.
        self.assertFalse(autopilot.carries_on(self.gh, self.cfg, decision, "agent_error", agent))
        self.gh.comment(
            9, render_run({**round_, "round": 3, "url": "f3", "attention": "out of time"})
        )
        self.sweep()
        self.assertEqual(len(self.started()), 2)
        _, [step] = autopilot.preview(self.gh, self.cfg)
        self.assertEqual((step["state"], step["reason"]), ("waits", "out of time"))
        self.assertTrue(self.gh.comments_matching(5, "@olivia a person is needed: out of time."))

    def test_a_revised_plan_with_questions_on_a_pull_request_is_answered_or_waits(self):
        self.with_pull(5, 9)
        round_ = {
            "trigger": "fix",
            "status": "failure",
            "outcome": "agent_error",
            "timed_out": True,
        }
        self.gh.comment(9, render_run({**round_, "round": 2, "url": "f2"}))
        self.sweep()
        self.posted_plan(5, questions=[{**QUESTION, "recommended": 0}])
        self.sweep()
        self.assertEqual(self.started(), [("plan", 5)])
        _, [step] = autopilot.preview(self.gh, self.cfg)
        self.assertEqual(step["state"], "waits")

    def test_doctor_says_when_nobody_hears_autopilot_stop(self):
        from nexkit.scaffold import _unheard

        [finding] = _unheard(pilot())
        self.assertIn("notify is empty", finding[1])
        self.assertEqual(_unheard(self.cfg), [])
        self.assertEqual(_unheard(make_config()), [])


class RecordTests(unittest.TestCase):
    def test_the_plan_comment_shows_recommendations_and_keeps_a_record_agents_never_see(self):
        from nexkit.publish import render_plan
        from nexkit.state import plan_data, plan_text

        body = render_plan(plan_output(questions=[QUESTION, "An old plain question?"]))
        self.assertIn("1. Which port?\n   - 8080 (recommended)\n   - 9090\n2. An old", body)
        comment = {"body": body, "user": {"login": "github-actions[bot]"}}
        data = plan_data(comment)
        self.assertEqual(data["questions"][0], RECORDED)
        self.assertEqual(data["questions"][1]["options"], [])
        self.assertFalse(data["too_large"])
        self.assertNotIn("nexkit:plan-data", plan_text(comment))
        self.assertIsNone(plan_data({**comment, "user": {"login": "mallory"}}))

    def test_a_blocked_round_shows_its_questions_and_keeps_them_in_its_marker(self):
        from tests.test_report import ReportCase, decision

        case = ReportCase("run")
        case.setUp()
        self.addCleanup(case.tearDown)
        conflict = {"file": "a.py", "base_change": "x", "pr_change": "y", **QUESTION}
        output = {"status": "blocked", "questions": [QUESTION], "open_conflicts": [conflict]}
        case.write(
            "nexkit-agent", "result.json", {"status": "blocked", "error": "Stuck", "output": output}
        )
        d = decision("fix", pr=6, head="c" * 40, target=6)
        outcome = case.run_report(d, published=False)
        self.assertEqual(outcome["outcome"], "agent_blocked")
        [run] = [c["run"] for c in case.gh.run_comments(6)]
        self.assertEqual(
            [q["question"] for q in run["questions"]], ["Which port?", "`a.py`: Which port?"]
        )
        [comment] = case.gh.run_comments(6)
        self.assertIn(
            "**Decisions needed**\n\n- Which port?\n  - 8080 (recommended)", comment["body"]
        )
        self.assertIn("  - **Question:** Which port?\n    - 8080 (recommended)", comment["body"])

    def test_an_agent_that_ran_out_of_time_says_so(self):
        from tests.support import interpret_claude

        result = interpret_claude("implement", None, timed_out=True)
        self.assertTrue(result["timed_out"])
        self.assertNotIn("timed_out", interpret_claude("implement", None))


class CITests(AutopilotCase):
    SHA = "e" * 40

    def setUp(self):
        super().setUp()
        self.cfg = make_config(
            autopilot={"enabled": True}, merge={"after_workflows": ["ci.yml"]}, notify=["olivia"]
        )
        self.issue(3)

    def ci_run(self, conclusion="failure", attempt=1, sha=SHA, run_id=50, days=0):
        run = {
            "id": run_id,
            "head_branch": "main",
            "head_repository": {"full_name": "acme/app"},
            "event": "push",
            "head_sha": sha,
            "status": "completed",
            "conclusion": conclusion,
            "run_attempt": attempt,
            "html_url": f"https://github.com/acme/app/actions/runs/{run_id}",
            "created_at": autopilot.iso(datetime.now(UTC) - timedelta(days=days)),
        }
        self.gh.workflow_history.setdefault("ci.yml", []).append(run)
        self.gh.jobs[run_id] = [
            {
                "id": run_id * 10 + 1,
                "name": "test",
                "conclusion": "failure",
                "html_url": "https://job",
                "steps": [{"name": "Run tests", "conclusion": "failure"}],
            },
            {"id": run_id * 10 + 2, "name": "lint", "conclusion": "success", "steps": []},
        ]
        return run

    def annotate(self, run_id, *names):
        """Error annotations on the test job of `run_id`: one per test name, then the
        runner's own."""
        self.gh.annotations[run_id * 10 + 1] = [
            *(
                {
                    "path": f"tests/{name.split('.')[0]}.cs",
                    "start_line": 10 + index,
                    "annotation_level": "failure",
                    "title": name,
                    "message": f"Assert.Equal() Failure in {name}",
                }
                for index, name in enumerate(names)
            ),
            GENERIC_ANNOTATION,
        ]

    def key(self, issue):
        return autopilot.flaky_failure(issue)

    def bugs(self):
        return [i for i in self.gh.issues.values() if autopilot.ci_workflow(i)]

    def flaky(self):
        return [i for i in self.gh.issues.values() if autopilot.flaky_workflow(i)]

    def passed_again(self, run, attempt=2):
        """`run` passed in `attempt` after every earlier attempt failed."""
        for earlier in range(1, attempt):
            self.gh.attempts[(run["id"], earlier)] = "failure"
        run.update(run_attempt=attempt, conclusion="success")
        self.gh.jobs[(run["id"], attempt)] = [{"name": "test", "conclusion": "success"}]

    def test_a_failure_runs_again_once_then_opens_a_bug_that_is_planned_first(self):
        run = self.ci_run()
        self.sweep()
        self.assertEqual(self.gh.reruns, [50])
        self.assertEqual(self.bugs(), [])
        run["run_attempt"] = 2
        self.gh.dispatches.clear()
        self.sweep()
        self.assertEqual(self.gh.reruns, [50])
        [bug] = self.bugs()
        self.assertEqual(bug["title"], 'Step "Run tests" of job "test" in ci.yml fails on main')
        self.assertEqual([x["name"] for x in bug["labels"]], ["bug"])
        self.assertEqual(bug["milestone"], {"number": 1})
        self.assertIn(
            "@olivia [`ci.yml` failed](https://github.com/acme/app/actions/runs/50)", bug["body"]
        )
        # The report lists every failed attempt.
        for attempt in (1, 2):
            url = f"https://github.com/acme/app/actions/runs/50/attempts/{attempt}"
            failed = f"Attempt [{attempt}]({url}):\n- [test](https://job): `Run tests`"
            self.assertIn(failed, bug["body"])
        self.assertNotIn("lint", bug["body"])
        self.assertTrue(bug["body"].endswith("<!-- nexkit:ci-runs 50.1 50.2 -->"))
        self.assertEqual(self.started(), [("plan", bug["number"])])
        _, steps = autopilot.preview(self.gh, self.cfg)
        self.assertEqual([s["number"] for s in steps], [bug["number"], 3])
        self.sweep()
        self.assertEqual((len(self.bugs()), self.flaky()), (1, []))
        self.assertEqual(self.gh.comments_matching(bug["number"], "nexkit:ci-runs"), [])

    def test_a_bug_issue_holds_the_annotations_and_the_end_of_each_failed_jobs_log(self):
        from nexkit.evidence import named_runs

        run = self.ci_run(attempt=2)
        self.annotate(50, "OrderTests.Totals")
        token = "ghp_" + "b" * 36
        log = "".join(f"2026-10-23T03:30:0{i}.1234567Z line {i}\n" for i in range(40))
        self.gh.logs[501] = f"{log}2026-10-23T03:31:00.0Z Error: boom {token}\n".encode()
        self.sweep()
        [bug] = self.bugs()
        body = bug["body"]
        self.assertIn("  - `OrderTests.Totals`, `tests/OrderTests.cs:10`", body)
        self.assertNotIn("Process completed", body)
        self.assertIn("  The end of its log:\n\n  ```text\n", body)
        self.assertIn("  Error: boom ***", body)
        self.assertNotIn(token, body)
        self.assertNotIn("line 9\n", body)  # only the last lines
        self.assertNotIn("2026-10-23T03:31", body)
        # A round on the bug issue collects that run's evidence, as its marker names it.
        runs, _ = named_runs(self.gh, bug, [])
        self.assertEqual(runs, [(run["id"], [1, 2])])

    def test_a_run_that_passes_when_run_again_is_recorded_once_on_its_own_issue(self):
        run = self.ci_run()
        self.sweep()
        self.passed_again(run)
        self.gh.dispatches.clear()
        self.sweep()
        self.assertEqual((self.gh.reruns, self.bugs()), ([50], []))
        [issue] = self.flaky()
        # Without annotations, the failure is named by its job and first failed step.
        title = 'Step "Run tests" of job "test" in ci.yml fails at times on main'
        self.assertEqual(issue["title"], title)
        self.assertEqual([x["name"] for x in issue["labels"]], ["bug"])
        self.assertEqual(issue["milestone"], {"number": 1})
        body = issue["body"]
        self.assertRegex(body, r"\A<!-- nexkit:ci-flaky ci\.yml [0-9a-f]{16} -->\n@olivia ")
        self.assertIn("in which step `Run tests` of job `test` failed:", body)
        self.assertIn(
            f"[A run of `ci.yml`](https://github.com/acme/app/actions/runs/50) on `main` at "
            f"{self.SHA} passed in attempt 2 after failing.",
            body,
        )
        url = "https://github.com/acme/app/actions/runs/50/attempts/1"
        self.assertIn(f"Attempt [{url[-1]}]({url}):\n- [test](https://job): `Run tests`", body)
        self.assertNotIn("attempts/2", body)
        self.assertTrue(body.endswith("<!-- nexkit:ci-runs 50.1 50.2 -->"))
        self.assertIn(("plan", issue["number"]), self.started())
        # A second sweep records nothing and reads no attempt again.
        calls = list(self.gh.calls)
        self.sweep()
        self.assertEqual(len(self.flaky()), 1)
        self.assertEqual(self.gh.comments_matching(issue["number"], "nexkit:ci-runs"), [])
        self.assertEqual(self.gh.calls, calls)
        # A failing default branch goes first, its flaky runs right after.
        self.ci_run(attempt=2, sha="f" * 40, run_id=51)
        self.sweep()
        [bug] = self.bugs()
        _, steps = autopilot.preview(self.gh, self.cfg)
        self.assertEqual([s["number"] for s in steps], [bug["number"], issue["number"], 3])
        # A later flaky run adds a comment to the open issue.
        self.passed_again(self.ci_run(sha="a" * 40, run_id=52), attempt=3)
        self.sweep()
        self.sweep()
        self.assertEqual(len(self.flaky()), 1)
        [comment] = self.gh.comments_matching(issue["number"], "nexkit:ci-runs")
        self.assertIn("runs/52) on `main` at " + "a" * 40 + " passed in attempt 3", comment["body"])
        self.assertIn("runs/52/attempts/2):\n- [test]", comment["body"])
        self.assertTrue(comment["body"].endswith("<!-- nexkit:ci-runs 52.1 52.2 52.3 -->"))
        self.assertEqual(self.gh.comments_matching(bug["number"], "runs/52"), [])

    def test_a_later_attempt_that_passes_after_the_bug_issue_says_so_there(self):
        run = self.ci_run(attempt=2)
        self.sweep()
        [bug] = self.bugs()
        # A person runs the jobs again a day later; a newer run passed meanwhile.
        run["created_at"] = autopilot.iso(datetime.now(UTC) - timedelta(days=3))
        self.ci_run(conclusion="success", sha="f" * 40, run_id=51)
        self.passed_again(run, attempt=3)
        self.sweep()
        self.sweep()
        [comment] = self.gh.comments_matching(bug["number"], "nexkit:ci-runs")
        self.assertIn(
            "Attempt 3 of [the run of `ci.yml`](https://github.com/acme/app/actions/runs/50) "
            f"at {self.SHA} passed: the failure did not repeat when run again.",
            comment["body"],
        )
        self.assertTrue(comment["body"].endswith("<!-- nexkit:ci-runs 50.3 -->"))
        self.assertEqual((self.flaky(), self.gh.reruns), ([], []))

    def test_an_older_failed_run_that_a_newer_run_overtook_is_recorded_not_run_again(self):
        self.ci_run(run_id=49, sha="d" * 40, days=2)
        self.ci_run()
        self.ci_run(conclusion=None, sha="f" * 40, run_id=51)["status"] = "in_progress"
        self.sweep()
        self.sweep()
        self.assertEqual((self.gh.reruns, self.bugs()), ([], []))
        [issue] = self.flaky()
        self.assertIn(
            f"[A run of `ci.yml`](https://github.com/acme/app/actions/runs/50) on `main` at "
            f"{self.SHA} failed, and a newer run overtook it.",
            issue["body"],
        )
        self.assertIn("runs/50/attempts/1):\n- [test](https://job): `Run tests`", issue["body"])
        self.assertTrue(issue["body"].endswith("<!-- nexkit:ci-runs 50.1 -->"))
        # A run from before the last day is left alone.
        self.assertNotIn("runs/49", issue["body"])
        self.assertEqual(self.gh.comments_matching(issue["number"], "nexkit:ci-runs"), [])

    def test_each_failure_of_a_flaky_run_gets_its_own_issue_named_by_its_annotation(self):
        name = (
            "ScenarioRunnerTests.Async_event_triggers_that_start_each_other_in_a_loop_fail_"
            "the_action_at_the_work_item_limit"
        )
        run = self.ci_run()
        self.annotate(50, name, "OrderTests.Totals_round_half_up")
        self.sweep()
        self.passed_again(run)
        self.sweep()
        first, second = sorted(self.flaky(), key=lambda i: i["number"])
        self.assertEqual(
            first["title"], f'Test "{name}" in ScenarioRunnerTests.cs fails at times on main'
        )
        self.assertEqual(
            second["title"],
            'Test "OrderTests.Totals_round_half_up" in OrderTests.cs fails at times on main',
        )
        self.assertNotEqual(self.key(first), self.key(second))
        for issue, test, line in (
            (first, name, 10),
            (second, "OrderTests.Totals_round_half_up", 11),
        ):
            body = issue["body"]
            self.assertIn(f"in which `{test}` failed:", body)
            self.assertIn(
                f"runs/50) on `main` at {self.SHA} passed in attempt 2 after failing.", body
            )
            url = "https://github.com/acme/app/actions/runs/50/attempts/1"
            self.assertIn(
                f"Attempt [1]({url}):\n- [test](https://job): `{test}`, "
                f"`tests/{test.split('.')[0]}.cs:{line}`: `Assert.Equal() Failure in {test}`",
                body,
            )
            self.assertTrue(body.endswith("<!-- nexkit:ci-runs 50.1 50.2 -->"))
            self.assertNotIn("Process completed", body)
        self.assertNotIn("OrderTests", first["body"])
        self.assertNotIn("ScenarioRunnerTests", second["body"])
        # The next sweep records neither again.
        self.sweep()
        self.assertEqual(len(self.flaky()), 2)
        self.assertEqual(self.gh.comments_matching(first["number"], "nexkit:ci-runs"), [])

    def test_a_failure_seen_again_is_recorded_on_its_issue_or_on_a_new_one_once_closed(self):
        name = "OrderTests.Totals_round_half_up"
        run = self.ci_run()
        self.annotate(50, name)
        self.passed_again(run)
        self.sweep()
        [issue] = self.flaky()
        # A later run with the same failure and a new one.
        later = self.ci_run(sha="a" * 40, run_id=52)
        self.annotate(52, name, "CartTests.Empty_cart")
        self.passed_again(later)
        self.sweep()
        [comment] = self.gh.comments_matching(issue["number"], "nexkit:ci-runs")
        self.assertIn("runs/52) on `main` at " + "a" * 40 + " passed in attempt 2", comment["body"])
        self.assertIn(f"- [test](https://job): `{name}`, `tests/OrderTests.cs:10`", comment["body"])
        self.assertNotIn("CartTests", comment["body"])
        self.assertTrue(comment["body"].endswith("<!-- nexkit:ci-runs 52.1 52.2 -->"))
        self.assertEqual(len(self.flaky()), 2)
        # Once its issue is closed, the failure opens a new issue that links the closed one.
        issue["state"] = "closed"
        again = self.ci_run(sha="b" * 40, run_id=53)
        self.annotate(53, name)
        self.passed_again(again)
        self.sweep()
        newest = max(self.flaky(), key=lambda i: i["number"])
        self.assertEqual((newest["title"], self.key(newest)), (issue["title"], self.key(issue)))
        self.assertIn(
            f"#{issue['number']}, now closed, was about the same failure.", newest["body"]
        )
        self.assertIn("runs/53) on `main`", newest["body"])
        self.assertTrue(newest["body"].endswith("<!-- nexkit:ci-runs 53.1 53.2 -->"))
        self.assertEqual(self.gh.comments_matching(issue["number"], "runs/53"), [])

    def test_a_failure_without_a_usable_annotation_is_named_by_its_job_and_failed_step(self):
        run = self.ci_run()
        self.gh.jobs[50][0]["steps"] = [
            {"name": "Restore", "conclusion": "success"},
            {"name": "Run tests", "conclusion": "failure"},
            {"name": "Upload results", "conclusion": "failure"},
        ]
        self.gh.annotations[501] = [
            GENERIC_ANNOTATION,
            # The workflow file and warnings name nothing either.
            {**GENERIC_ANNOTATION, "path": ".github/workflows/ci.yml", "message": "Bad input"},
            {**GENERIC_ANNOTATION, "annotation_level": "warning", "title": "Deprecated"},
        ]
        self.passed_again(run)
        self.sweep()
        [issue] = self.flaky()
        title = 'Step "Run tests" of job "test" in ci.yml fails at times on main'
        self.assertEqual(issue["title"], title)
        self.assertIn("- [test](https://job): `Run tests`, `Upload results`\n", issue["body"])
        for text in ("Process completed", "Bad input", "Deprecated"):
            self.assertNotIn(text, issue["body"])
        # A failed job without a failed step is named by the job alone.
        later = self.ci_run(sha="a" * 40, run_id=52)
        self.gh.jobs[52][0]["steps"] = []
        self.passed_again(later)
        self.sweep()
        newest = max(self.flaky(), key=lambda i: i["number"])
        self.assertEqual(newest["title"], 'Job "test" in ci.yml fails at times on main')
        self.assertIn("in which job `test` failed:", newest["body"])

    def test_titles_name_what_fails_and_where_in_plain_words(self):
        spec = "e2e/purchase-request-release.spec.ts"

        def test(title, path=spec):
            note = {"annotation_level": "failure", "title": title, "path": path, "start_line": 12}
            return autopilot._annotated("[e2e](https://job)", note)

        def job(name, *steps):
            failed = [{"name": step, "conclusion": "failure"} for step in steps]
            return autopilot._job_failure("ci.yml", {"name": name, "steps": failed}, "job")

        prefix = "[chromium] › purchase-request-release.spec.ts:12:5 › purchase request release › "
        short = "an author publishes from Studio and the site shows the new release"
        long = (
            "an author takes a change from edit to preview, publish and rollback without the "
            "admin CLI, and a waiting request finishes on its release"
        )
        file = "in purchase-request-release.spec.ts fails at times on main"
        for failure, title in (
            (test(prefix + short), f'Test "{short}" {file}'),
            (test(prefix + long), f'Test "{long}" {file}'),
            (
                test(
                    "RecordHistory > pages through more than twenty entries",
                    "src/x/RecordHistory.test.tsx",
                ),
                'Test "pages through more than twenty entries" in RecordHistory.test.tsx '
                "fails at times on main",
            ),
            (job("Integration tests"), 'Job "Integration tests" in ci.yml fails at times on main'),
            (
                job("Integration tests", "Integration tests"),
                'Job "Integration tests" in ci.yml fails at times on main',
            ),
            (
                job("Lint and unit tests", "Unit tests", "Upload results"),
                'Step "Unit tests" of job "Lint and unit tests" in ci.yml fails at times on main',
            ),
            (test(""), f"Line 12 of {spec} fails at times on main"),
            (test('says "hi" in `code`'), f"Test \"says 'hi' in 'code'\" {file}"),
        ):
            self.assertEqual(autopilot.ci_title(failure, "main"), title)
        # The body keeps the whole annotation title, its file and line, with backticks.
        self.assertEqual(
            test(prefix + short)["line"], f"- [e2e](https://job): `{prefix + short}`, `{spec}:12`"
        )
        # A red branch names what failed in its latest attempt.
        steps = [job("Static checks", "Lint"), job("Lint and unit tests", "Unit tests")]
        self.assertEqual(
            autopilot.red_title("ci.yml", steps, "main"),
            'Steps "Lint" and "Unit tests" in ci.yml fail on main',
        )
        self.assertEqual(
            autopilot.red_title("ci.yml", steps[1:], "main"),
            'Step "Unit tests" of job "Lint and unit tests" in ci.yml fails on main',
        )
        mixed = [*steps, test(prefix + short)]
        self.assertEqual(
            autopilot.red_title("ci.yml", mixed, "main"), "ci.yml fails in 3 ways on main"
        )
        nothing = mock.Mock(attempt_jobs=lambda *_: [])
        [start] = autopilot._attempt_failures(nothing, "ci.yml", {"id": 50}, 1)
        self.assertEqual(
            autopilot.red_title("ci.yml", [start], "main"), "ci.yml does not start on main"
        )
        self.assertEqual(
            autopilot.ci_title(start, "main"), "ci.yml does not start at times on main"
        )
        # Only past GitHub's limit is a title cut, at a word boundary and without "…".
        words = " ".join(f"step{n}" for n in range(60))
        title = autopilot.ci_title(test(f"Suite > {words}"), "main")
        self.assertLessEqual(len(title), autopilot.CI_TITLE)
        self.assertTrue(f'Test "{words}"'.startswith(title))
        self.assertRegex(title, r"step\d+\Z")

    def test_a_failure_whose_issue_has_a_title_from_before_is_still_recorded_there(self):
        name = "OrderTests.Totals_round_half_up"
        key = autopilot._key("annotation", name)
        old = self.gh.create_issue(
            f"`{name}` fails at times on `main`",
            f"<!-- nexkit:ci-flaky ci.yml {key} -->\nRecords.\n\n<!-- nexkit:ci-runs 49.1 -->",
            1,
            ["bug"],
        )
        run = self.ci_run()
        self.annotate(50, name)
        self.passed_again(run)
        self.sweep()
        self.assertEqual(self.flaky(), [old])
        [comment] = self.gh.comments_matching(old["number"], "nexkit:ci-runs")
        self.assertIn("runs/50) on `main`", comment["body"])

    def test_a_red_branch_with_two_failed_steps_names_both(self):
        self.ci_run(attempt=2)
        self.gh.jobs[50][1].update(
            name="Static checks",
            conclusion="failure",
            steps=[{"name": "Lint", "conclusion": "failure"}],
        )
        self.gh.jobs[50][0]["steps"][0]["name"] = "Unit tests"
        self.sweep()
        [bug] = self.bugs()
        self.assertEqual(bug["title"], 'Steps "Unit tests" and "Lint" in ci.yml fail on main')

    def test_a_run_with_too_many_new_failures_records_them_on_one_issue_for_the_run(self):
        known = "OrderTests.Totals_round_half_up"
        run = self.ci_run()
        self.annotate(50, known)
        self.passed_again(run)
        self.sweep()
        [issue] = self.flaky()
        broken = [f"BrokenTests.Case_{n}" for n in range(autopilot.CI_NEW_ISSUES + 1)]
        later = self.ci_run(sha="a" * 40, run_id=52)
        self.annotate(52, known, *broken)
        self.passed_again(later)
        self.sweep()
        # The failure with an issue gets its record there; the new ones share one issue.
        self.assertEqual(len(self.gh.comments_matching(issue["number"], "runs/52")), 1)
        [for_run] = [i for i in self.flaky() if i is not issue]
        self.assertEqual(for_run["title"], "Run 52 of ci.yml failed in 6 ways at once on main")
        for name in broken:
            self.assertIn(f"`{name}`, `tests/BrokenTests.cs:", for_run["body"])
        self.assertNotIn(known, for_run["body"])
        self.assertTrue(for_run["body"].endswith("<!-- nexkit:ci-runs 52.1 52.2 -->"))

    def test_an_issue_from_4_3_0_keeps_its_records_and_gets_no_new_ones(self):
        old = self.gh.create_issue(
            "`ci.yml` fails at times on `main`",
            "<!-- nexkit:ci-flaky ci.yml -->\nRecords.\n\n<!-- nexkit:ci-runs 50.1 50.2 -->",
            1,
            ["bug"],
        )
        self.passed_again(self.ci_run())
        self.passed_again(self.ci_run(sha="a" * 40, run_id=52))
        self.sweep()
        self.assertEqual((autopilot.flaky_workflow(old), self.key(old)), ("ci.yml", None))
        self.assertEqual(self.gh.comments_matching(old["number"], "nexkit:ci-runs"), [])
        [new] = [i for i in self.flaky() if self.key(i)]
        self.assertIn("runs/52) on `main`", new["body"])
        self.assertNotIn("runs/50)", new["body"])
        # Both are still planned right after the issues for a failing default branch.
        _, steps = autopilot.preview(self.gh, self.cfg)
        self.assertEqual([s["number"] for s in steps], [old["number"], new["number"], 3])

    def test_a_newer_failing_commit_comments_on_the_open_bug(self):
        self.ci_run(attempt=2)
        self.sweep()
        [bug] = self.bugs()
        self.ci_run(attempt=2, sha="f" * 40, run_id=51)
        self.sweep()
        self.sweep()
        self.assertEqual(len(self.bugs()), 1)
        [comment] = self.gh.comments_matching(bug["number"], "fails again on a newer commit")
        self.assertIn("f" * 40, comment["body"])
        # Closed without a fix, the same commit is not reported again.
        bug["state"] = "closed"
        self.sweep()
        self.assertEqual(len(self.bugs()), 1)

    def test_passing_or_running_ci_and_other_branches_are_left_alone(self):
        self.ci_run(conclusion="success")
        self.ci_run(run_id=52)["head_branch"] = "nexkit/issue-3"
        running = self.ci_run(run_id=53, conclusion=None)
        running["status"] = "in_progress"
        # Runs that passed at once lead to nothing, so the sweep reads no issue for them.
        with mock.patch.object(autopilot, "_ci_records", side_effect=AssertionError):
            self.sweep()
        self.assertEqual((self.gh.reruns, self.bugs()), ([], []))
        self.gh.workflow_history.clear()
        self.ci_run(attempt=2)
        self.sweep(pilot())  # no merge.after_workflows
        self.assertEqual(self.bugs(), [])

    def test_runs_of_pull_requests_with_a_head_branch_named_main_are_left_alone(self):
        run = self.ci_run(run_id=48, sha="d" * 40)
        run["event"] = "workflow_dispatch"
        # Pull requests from a fork whose branch is also named `main`, and one from this
        # repository's `main` into another branch.
        fork = {"full_name": "mallory/app"}
        self.ci_run(attempt=2, run_id=49).update(event="pull_request", head_repository=fork)
        self.ci_run(run_id=50).update(event="pull_request_target", head_repository=fork)
        self.ci_run(attempt=2, sha="f" * 40, run_id=51)["event"] = "pull_request"
        self.sweep()
        # The run of the default branch is still the latest, and the only one run again.
        self.assertEqual(self.gh.reruns, [48])
        run["run_attempt"] = 2
        self.sweep()
        self.sweep()
        [bug] = self.bugs()
        self.assertTrue(bug["body"].endswith("<!-- nexkit:ci-runs 48.1 48.2 -->"))
        for sha in (self.SHA, "f" * 40):
            self.assertNotIn(sha, bug["body"])
        self.assertEqual((self.flaky(), self.gh.reruns), ([], [48]))

    def test_the_null_scope_gives_the_bug_no_milestone(self):
        self.ci_run(attempt=2)
        cfg = make_config(
            autopilot={"enabled": True, "scope": None},
            merge={"after_workflows": ["ci.yml"]},
        )
        self.sweep(cfg)
        [bug] = self.bugs()
        self.assertEqual([x["name"] for x in bug["labels"]], ["bug"])
        self.assertIsNone(bug["milestone"])
        self.assertEqual(self.started()[0], ("plan", bug["number"]))

    def test_only_nexkits_own_bug_issues_go_first(self):
        self.issue(4, body="<!-- nexkit:ci ci.yml -->\nForged.")
        self.issue(5, body="<!-- nexkit:ci-flaky ci.yml -->\nForged.")
        self.assertIsNone(autopilot.ci_workflow(self.gh.issues[4]))
        self.assertIsNone(autopilot.flaky_workflow(self.gh.issues[5]))
        _, steps = autopilot.preview(self.gh, self.cfg)
        self.assertEqual([s["number"] for s in steps], [3, 4, 5])
