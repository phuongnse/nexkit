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


QUESTION = {"question": "Which port?", "options": ["8080", "9090"], "recommended": "8080"}


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
        self.posted_plan(4, questions=[{**QUESTION, "recommended": ""}])
        self.issue(5)
        self.posted_plan(5, questions=[QUESTION])
        _, steps = autopilot.preview(self.gh, self.cfg)
        self.assertEqual([s["state"] for s in steps], ["waits", "start"])
        _, steps = autopilot.preview(self.gh, pilot())  # answer: "never"
        self.assertEqual([s["state"] for s in steps], ["waits", "waits"])
        # A recommendation that is not one of the options is none.
        self.assertIsNone(autopilot.answers(self.cfg, [{**QUESTION, "recommended": "80"}]))

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

    def test_report_mentions_nobody_when_autopilot_answers(self):
        cfg = make_config(autopilot={"enabled": True, "answer": "recommended"}, notify=["olivia"])
        self.issue(5)
        result = self.report_plan(cfg, plan_output(questions=[QUESTION]))
        self.assertIsNone(result.get("attention"))
        self.assertIn("NexKit autopilot carries on from here.", result["summary"])
        self.assertFalse(self.gh.comments_matching(5, "@olivia"))
        self.report_plan(cfg, plan_output(questions=[{**QUESTION, "recommended": ""}]))
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


class RecordTests(unittest.TestCase):
    def test_the_plan_comment_shows_recommendations_and_keeps_a_record_agents_never_see(self):
        from nexkit.publish import render_plan
        from nexkit.state import plan_data, plan_text

        body = render_plan(plan_output(questions=[QUESTION, "An old plain question?"]))
        self.assertIn("1. Which port?\n   - 8080 (recommended)\n   - 9090\n2. An old", body)
        comment = {"body": body, "user": {"login": "github-actions[bot]"}}
        data = plan_data(comment)
        self.assertEqual(data["questions"][0], QUESTION)
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

    def ci_run(self, conclusion="failure", attempt=1, sha=SHA, run_id=50):
        run = {
            "id": run_id,
            "head_branch": "main",
            "head_sha": sha,
            "status": "completed",
            "conclusion": conclusion,
            "run_attempt": attempt,
            "html_url": f"https://github.com/acme/app/actions/runs/{run_id}",
        }
        self.gh.workflow_history.setdefault("ci.yml", []).append(run)
        self.gh.jobs[run_id] = [
            {
                "name": "test",
                "conclusion": "failure",
                "html_url": "https://job",
                "steps": [{"name": "Run tests", "conclusion": "failure"}],
            },
            {"name": "lint", "conclusion": "success", "steps": []},
        ]
        return run

    def bugs(self):
        return [i for i in self.gh.issues.values() if autopilot.ci_workflow(i)]

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
        self.assertEqual(bug["title"], "`ci.yml` fails on `main`")
        self.assertEqual([x["name"] for x in bug["labels"]], ["bug"])
        self.assertEqual(bug["milestone"], {"number": 1})
        self.assertIn(
            "@olivia [`ci.yml` failed](https://github.com/acme/app/actions/runs/50)", bug["body"]
        )
        self.assertIn("- [test](https://job): `Run tests`", bug["body"])
        self.assertNotIn("lint", bug["body"])
        self.assertEqual(self.started(), [("plan", bug["number"])])
        _, steps = autopilot.preview(self.gh, self.cfg)
        self.assertEqual([s["number"] for s in steps], [bug["number"], 3])
        self.sweep()
        self.assertEqual(len(self.bugs()), 1)

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
        self.sweep()
        self.assertEqual((self.gh.reruns, self.bugs()), ([], []))
        self.gh.workflow_history.clear()
        self.ci_run(attempt=2)
        self.sweep(pilot())  # no merge.after_workflows
        self.assertEqual(self.bugs(), [])

    def test_the_label_scope_labels_the_bug(self):
        self.ci_run(attempt=2)
        cfg = make_config(
            autopilot={"enabled": True, "scope": "label", "label": "go"},
            merge={"after_workflows": ["ci.yml"]},
        )
        self.sweep(cfg)
        [bug] = self.bugs()
        self.assertEqual([x["name"] for x in bug["labels"]], ["bug", "go"])
        self.assertIsNone(bug["milestone"])
        self.assertEqual(self.started(), [("plan", bug["number"])])

    def test_only_nexkits_own_bug_issues_go_first(self):
        self.issue(4, body="<!-- nexkit:ci ci.yml -->\nForged.")
        self.assertIsNone(autopilot.ci_workflow(self.gh.issues[4]))
