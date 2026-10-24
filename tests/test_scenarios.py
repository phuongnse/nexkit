"""Autopilot played over many rounds.

Each scenario scripts what each run's agent, checks and review return, and plays the
dispatches through the real `route`, `progress`, `report` and autopilot sweeps against an
in-memory GitHub, with NexKit's real comments and markers. After every sweep, and once
nothing is left to run, it checks the invariant: every issue in scope is either carried
on by a run, or waits for a person, listed under *Needs a person* with the same reason and
mentioned to `notify`; and automatic rounds count only rounds that ran their agent to its
end or published.
"""

from __future__ import annotations

import base64
import itertools
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from nexkit import autopilot, progress
from nexkit.github import GitHubError
from nexkit.maintain import maintain
from nexkit.publish import publish_plan
from nexkit.report import report
from nexkit.route import route
from nexkit.state import BOT_LOGIN, UNCOUNTED, read_state, run_comments
from nexkit.status import collect
from tests.support import FakeGitHub, make_config

WORKFLOW = "acme/app/.github/workflows/nexkit.yml@refs/heads/main"
# More dispatches than this in one scenario is a loop.
MAX_DISPATCHES = 60
TOKENS = {"input": 10, "cached": 0, "output": 10}


def plan(questions=(), too_large=False, split=()):
    output = {
        "summary": "Fix it.",
        "changes": ["It is fixed."],
        "decisions": [],
        "risks": [],
        "acceptance_criteria": ["A test shows it."],
        "implementation_notes": ["Edit app.py."],
        "questions": list(questions),
        "too_large": too_large,
        "split": list(split),
        "revision": {"started_over": False, "changes": []},
    }
    return {"agent": {"status": "done", "output": output, "tokens": TOKENS}}


def done(checks=True, verdict="approve"):
    findings = (
        []
        if verdict == "approve"
        else [{"severity": "blocking", "file": "", "line": 0, "body": "No."}]
    )
    review = {
        "status": "done",
        "tokens": TOKENS,
        "output": {"verdict": verdict, "summary": "Read it.", "criteria": [], "findings": findings},
    }
    check = {"name": "test", "run": "make test", "exit_code": 0 if checks else 1, "passed": checks}
    check["output"] = "" if checks else "AssertionError: same failure"
    return {
        "agent": {"status": "done", "summary": "Fixed.", "tokens": TOKENS},
        "checks": [check],
        "review": review,
    }


def blocked(recommended=1):
    question = {
        "question": "Who downloads the artifacts?",
        "options": ["Someone with gh downloads them", "Skip"],
        "recommended": recommended,
    }
    output = {"status": "blocked", "blocker": "No artifacts.", "questions": [question]}
    return {"agent": {"status": "blocked", "error": "No artifacts.", "output": output}}


def error(**fields):
    return {"agent": {"status": "error", "error": "Claude stopped.", **fields}}


class Pipeline:
    """The in-memory GitHub, the dispatches still to run, and the scripted outcomes."""

    def __init__(self, test, cfg, script):
        self.test = test
        self.gh = FakeGitHub()
        self.cfg = cfg
        self.script = script
        self.handled = 0
        self.runs = itertools.count(100)
        self.heads = itertools.count(1)
        self.played = []  # (command, number, outcome) for each run
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "acme/app"})
        env.start()
        test.addCleanup(env.stop)
        for patched in ("nexkit.report.time.sleep", "nexkit.maintain.time.sleep"):
            sleep = mock.patch(patched)
            sleep.start()
            test.addCleanup(sleep.stop)
        self.gh.add_milestone(1)

    def issue(self, number, **kw):
        return self.gh.add_issue(number, title=f"Issue {number}", milestone=1, **kw)

    # -- playing ------------------------------------------------------------------

    def play(self):
        """Run every dispatch, and the ones they start, then the hourly sweeps that time
        brings, until nothing changes. Returns the commands that ran."""
        autopilot.request(self.gh, "nexkit.yml", "main")
        self.drain()
        for _ in range(3):
            # An hour, then seven: what only time moves on, such as a lost command.
            self.later(timedelta(hours=7))
            before = len(self.gh.dispatches)
            autopilot.request(self.gh, "nexkit.yml", "main")
            self.drain()
            if len(self.gh.dispatches) == before + 1:
                break
        self.check(quiet=True)
        return [(command, number) for command, number, _ in self.played]

    def drain(self):
        while self.handled < len(self.gh.dispatches):
            self.test.assertLess(self.handled, MAX_DISPATCHES, f"a loop: {self.played}")
            inputs = self.gh.dispatches[self.handled]["inputs"]
            self.handled += 1
            if inputs["command"] == "autopilot":
                note = str(inputs.get("note") or "")
                autopilot.sweep(
                    self.gh, self.cfg, "nexkit.yml", "main", int(note) if note.isdigit() else None
                )
                self.check()
            else:
                self.run(inputs)

    def run(self, inputs):
        outcome = self.script(self, inputs)
        if outcome == "lost":
            # GitHub dropped it: no run, no comment.
            self.played.append((inputs["command"], int(inputs["number"]), "lost"))
            return
        run_id = next(self.runs)
        os.environ["GITHUB_RUN_ID"] = str(run_id)
        decision = route(self.gh, "workflow_dispatch", {"sender": {"login": BOT_LOGIN}}, inputs)
        if decision["action"] == "none":
            self.played.append((inputs["command"], int(inputs["number"]), decision["reason"]))
            return
        progress.start(self.gh, decision)
        if outcome == "stall":
            # The runner was lost: the run ended, and its report never ran.
            self.gh.runs[run_id] = {"status": "completed", "conclusion": "failure"}
            self.played.append((inputs["command"], int(inputs["number"]), "stall"))
            return
        self.gh.runs[run_id] = {"status": "in_progress"}
        artifacts = Path(self.tmp.name) / str(run_id)
        needs = self.stages(decision, outcome, artifacts)
        result = report(
            self.gh,
            decision,
            self.cfg,
            needs,
            artifacts,
            workflow_ref=WORKFLOW,
            default_branch="main",
        )
        self.gh.runs[run_id] = {"status": "completed"}
        self.played.append((inputs["command"], decision["target"], result["outcome"]))

    def stages(self, decision, outcome, artifacts):
        """Write what the agent, checks and review jobs leave, and publish a plan, a pull
        request or a new head as `publish` would. Returns `needs`."""

        def write(name, data):
            (artifacts / name).mkdir(parents=True, exist_ok=True)
            (
                artifacts / name / "result.json"
                if name != "nexkit-checks"
                else artifacts / name / "checks.json"
            ).write_text(json.dumps(data))

        agent = outcome.get("agent")
        if agent is not None and decision["action"] != "review":
            write("nexkit-agent", {"stage": decision["action"], **agent})
        needs = {}
        published = {"published": False}
        if decision["action"] == "plan" and agent and agent["status"] == "done":
            context = {"title": "x"}
            published = publish_plan(self.gh, decision, agent, context, self.cfg)
        elif decision["action"] == "implement" and agent and agent.get("no_change"):
            published = {"published": False, "no_change": True}
        elif decision["action"] == "implement" and agent and agent["status"] == "done":
            pull = self.gh.create_pull("x", decision["branch"], "main", "Closes it.")
            published = {"published": True, "pr": pull["number"], "head": self.head(pull["number"])}
            if outcome.get("meanwhile"):
                # What happens elsewhere while the round checks and reviews its pull request.
                outcome["meanwhile"](self, pull["number"])
        elif decision["action"] == "fix" and agent and agent.get("recheck"):
            published = {"published": False, "recheck": True, "pr": decision["pr"]}
            published["head"] = decision["head"]
        elif decision["action"] == "fix" and agent and agent["status"] == "done":
            published = {"published": True, "pr": decision["pr"], "head": self.head(decision["pr"])}
        published.update(outcome.get("publish") or {})
        needs["publish"] = {"outputs": {"result": json.dumps(published)}}
        reviewing = decision["action"] == "review" or published.get("head")
        if reviewing and "checks" in outcome:
            write("nexkit-checks", outcome["checks"])
        if reviewing and "review" in outcome:
            write("nexkit-review", outcome["review"])
        return needs

    def head(self, pull):
        sha = f"{next(self.heads):040x}"
        self.gh.pulls[pull]["head"]["sha"] = sha
        return sha

    def later(self, step):
        """Move every record and comment of NexKit back by `step`, as if that much time
        passed."""
        for comments in self.gh.issue_comments.values():
            for comment in comments:
                for key in ("created_at", "updated_at"):
                    at = datetime.fromisoformat(
                        (comment.get(key) or "2026-01-01T00:00:00Z").replace("Z", "+00:00")
                    )
                    comment[key] = (at - step).strftime("%Y-%m-%dT%H:%M:%SZ")
                match = autopilot.RECORD_PATTERN.search(comment["body"])
                if match and comment["user"]["login"] == BOT_LOGIN:
                    record = json.loads(base64.b64decode(match.group(1)))
                    at = datetime.fromisoformat(record["at"].replace("Z", "+00:00")) - step
                    record["at"] = autopilot.iso(at)
                    comment["body"] = autopilot.RECORD_PATTERN.sub(
                        autopilot.record_marker(record), comment["body"]
                    )

    # -- the invariant --------------------------------------------------------------

    def check(self, quiet=False):
        """Every issue in scope is carried on, or waits for a person, listed and mentioned;
        when nothing is left to run, none is carried on by a run that does not exist."""
        _, steps = autopilot.preview(self.gh, self.cfg)
        data = collect(self.gh, self.cfg, workers=1)
        listed = {(item["number"], item["reason"]) for item in data["needs_person"]}
        for step in steps:
            where = f"#{step['number']} {step['state']}: {step['reason']} after {self.played}"
            if step["state"] == "waits" and step.get("person"):
                numbers = {step["number"], step.get("pull")}
                self.test.assertTrue(
                    any((n, step["reason"]) in listed for n in numbers), f"not listed: {where}"
                )
                if self.cfg["notify"]:
                    self.test.assertTrue(step.get("mentioned"), f"nobody mentioned: {where}")
            if quiet:
                self.test.assertTrue(
                    step["state"] == "waits" and step.get("person"),
                    f"stuck with nothing running: {where}",
                )
        for pull in self.gh.pulls.values():
            self.budget(pull["number"])

    def budget(self, pull):
        """`auto_fixes` counts the automatic fix rounds that ran, none that stopped outside
        the agent's work or were the one more try, and stays within the limit."""
        comments = self.gh.comments(pull)
        _, state = read_state(comments)
        if not state:
            return
        rounds = [run for _, run in run_comments(comments) if "round" in run]
        ran = [
            run
            for run in rounds
            if run["trigger"].startswith("fix (")
            and "auto" in run["trigger"][5:-1].split(", ")
            and not run.get("conflicts")
            and not run.get("retry")
            and run.get("outcome") not in UNCOUNTED
        ]
        waiting = sum(
            1
            for d in self.gh.dispatches[self.handled :]
            if d["inputs"]["command"] == "fix" and d["inputs"]["auto"] == "true"
        )
        self.test.assertLessEqual(state["auto_fixes"], self.cfg["fix"]["max_auto_rounds"])
        self.test.assertEqual(state["auto_fixes"], len(ran) + waiting, f"#{pull}: {self.played}")


def config(**kw):
    autopilot_settings = {"enabled": True, "answer": "recommended", **kw.pop("autopilot", {})}
    return make_config(
        autopilot=autopilot_settings,
        merge={"auto": True},
        conflicts={"auto_resolve": True},
        notify=["olivia"],
        **kw,
    )


class ScenarioTests(unittest.TestCase):
    def test_an_issue_is_planned_implemented_reviewed_and_merged(self):
        def script(pipe, inputs):
            return plan() if inputs["command"] == "plan" else done()

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5)])
        self.assertEqual(pipe.gh.issues[5]["state"], "closed")
        self.assertTrue(pipe.gh.comments_matching(5, "NexKit autopilot finished milestone M1"))

    def test_axis_1089_a_round_that_blocks_after_autopilots_answer_waits(self):
        # Every go blocks, recommending that a person download the artifacts.
        def script(pipe, inputs):
            return plan() if inputs["command"] == "plan" else blocked()

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5), ("go", 5)])
        self.assertEqual(len(pipe.gh.comments_matching(5, "answers for the owner")), 1)
        self.assertEqual(len(pipe.gh.comments_matching(5, "@olivia a person is needed")), 1)

    def test_axis_1091_a_fix_round_held_up_by_workflow_changes_keeps_its_rounds(self):
        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return plan()
            if inputs["command"] == "go":
                return done(checks=False)
            return {"agent": {"status": "needs_merge", "error": "Merging `main` brings changes."}}

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5), ("fix", 6)])
        self.assertEqual(read_state(pipe.gh.comments(6))[1]["auto_fixes"], 0)

    def test_checks_that_keep_failing_use_their_rounds_then_wait(self):
        def script(pipe, inputs):
            return plan() if inputs["command"] == "plan" else done(checks=False)

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        played = pipe.play()
        self.assertEqual(played, [("plan", 5), ("go", 5), ("fix", 6), ("fix", 6)])

    def test_a_transient_error_runs_once_more_and_twice_waits(self):
        tries = itertools.count()

        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return plan()
            return error(transient=True) if next(tries) < 1 else done()

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5), ("go", 5)])
        self.assertEqual(pipe.gh.issues[5]["state"], "closed")

        def always(pipe, inputs):
            return plan() if inputs["command"] == "plan" else error(transient=True)

        pipe = Pipeline(self, config(), always)
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5), ("go", 5)])

    def test_a_command_that_never_runs_starts_once_more_then_waits(self):
        def script(pipe, inputs):
            return "lost"

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5), ("plan", 5)])

    def test_a_run_that_ends_without_a_report_starts_once_more(self):
        stalls = itertools.count()

        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return "stall" if next(stalls) < 1 else plan()
            return done()

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5), ("plan", 5), ("go", 5)])
        self.assertEqual(pipe.gh.issues[5]["state"], "closed")

    def test_a_merge_that_waits_for_required_checks_merges_when_they_pass(self):
        def script(pipe, inputs):
            return plan() if inputs["command"] == "plan" else done()

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        pipe.gh.merge_error = [GitHubError(405, 'Required status check "e2e" is expected.')]
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5)])
        self.assertEqual(pipe.gh.issues[5]["state"], "closed")

    def test_axis_1129_a_base_branch_move_waits_for_the_implement_round(self):
        pushed = []

        def move_base(pipe, pull):
            # A person merges into `main` while the round reviews; the pull request now
            # conflicts, and the push starts NexKit's conflict check.
            pipe.gh.branches["main"] = "e" * 40
            pipe.gh.pulls[pull]["mergeable"] = False
            pipe.gh.merge_error = [GitHubError(405, "Pull Request is not mergeable")]
            event = {"ref": "refs/heads/main", "repository": {"default_branch": "main"}}
            before = len(pipe.gh.dispatches)
            maintain(
                pipe.gh, route(pipe.gh, "push", event), pipe.cfg, workflow="nexkit.yml", ref="main"
            )
            pushed.append(pipe.gh.dispatches[before:])

        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return plan()
            if inputs["command"] == "go":
                return {**done(), "meanwhile": move_base}
            # The conflict round merges `main`.
            pipe.gh.pulls[int(inputs["number"])]["mergeable"] = True
            return done()

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5), ("fix", 6)])
        # The push started nothing while the implement round ran; its refused merge started
        # the conflict round, which merged.
        self.assertEqual(pushed, [[]])
        self.assertEqual([outcome for *_, outcome in pipe.played[1:]], ["conflict_fix", "merged"])
        self.assertEqual(pipe.gh.issues[5]["state"], "closed")
        for number in (5, 6):
            self.assertFalse(pipe.gh.comments_matching(number, "a person is needed"))

    def test_a_merge_refused_because_the_head_moved_reviews_the_new_head(self):
        def push(pipe, pull):
            # A person pushes to the branch while the round reviews.
            pipe.gh.merge_error = [GitHubError(409, "Head branch was modified.")]
            pipe.head(pull)

        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return plan()
            if inputs["command"] == "go":
                return {**done(), "meanwhile": push}
            return done()

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5), ("review", 6)])
        self.assertEqual([outcome for *_, outcome in pipe.played[1:]], ["head_moved", "merged"])
        for number in (5, 6):
            self.assertFalse(pipe.gh.comments_matching(number, "a person is needed"))

    def test_axis_1104_a_fix_round_with_a_cause_outside_the_code_checks_again_once(self):
        cause = "The registry returned 500 Internal Server Error during `docker compose up`."
        outside = {"cause": cause, "cause_outside": True}

        def script(passes):
            def play(pipe, inputs):
                if inputs["command"] == "plan":
                    return plan()
                if inputs["command"] == "go":
                    return done(checks=False)
                found = {**done(checks=passes), "recheck": True}
                found["agent"] = {**found["agent"], "recheck": True, "output": outside}
                return found

            return play

        pipe = Pipeline(self, config(), script(passes=True))
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5), ("fix", 6)])
        self.assertEqual(pipe.gh.issues[5]["state"], "closed")
        self.assertEqual(read_state(pipe.gh.comments(6))[1]["auto_fixes"], 1)
        # When the checks still fail on the same commit, a person decides, and sees what
        # the agent found.
        pipe = Pipeline(self, config(), script(passes=False))
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5), ("fix", 6)])
        self.assertEqual(pipe.played[-1][2], "recheck_failed")
        [mention] = pipe.gh.comments_matching(6, "a person is needed")
        self.assertIn(cause.rstrip("."), mention["body"])

    def test_axis_1089_an_answer_that_only_closes_the_issue_closes_it(self):
        finding = "8b9cb87 already fixed it: the last 20 runs of `e2e` on `main` passed."

        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return plan()
            question = {
                "question": "The failure is already fixed. Close the issue?",
                "options": ["Close it as fixed by 8b9cb87", "Keep it open"],
                "recommended": 1,
            }
            output = {"status": "blocked", "blocker": "Already fixed.", "questions": [question]}
            output["no_change"] = finding
            return {"agent": {"status": "blocked", "error": "Already fixed.", "output": output}}

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5)])
        self.assertEqual(pipe.gh.issues[5]["state"], "closed")
        [closing] = pipe.gh.comments_matching(5, "NexKit autopilot closes this issue")
        self.assertIn(finding, closing["body"])
        self.assertFalse(pipe.gh.comments_matching(5, "a person is needed"))
        self.assertFalse(pipe.gh.comments_matching(5, "answers for the owner"))

    def test_a_round_that_finds_no_change_needed_closes_the_issue(self):
        finding = "The page already shows the release: test_release passes on main."

        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return plan()
            output = {"status": "done", "summary": "Nothing to change.", "no_change": finding}
            return {"agent": {"status": "done", "no_change": True, "output": output}}

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5)])
        self.assertEqual(pipe.played[-1][2], "no_change")
        self.assertEqual(pipe.gh.issues[5]["state"], "closed")
        self.assertEqual(pipe.gh.pulls, {})
        [run] = [
            comment
            for comment, _ in run_comments(pipe.gh.comments(5))
            if finding in comment["body"]
        ]
        self.assertIn("No change is needed, so NexKit closed #5 as completed.", run["body"])

    def test_a_fix_round_out_of_time_replans_then_fixes_then_waits(self):
        fixes = itertools.count()

        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return plan()
            if inputs["command"] == "go":
                return done(checks=False)
            next(fixes)
            return error(timed_out=True)

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        played = pipe.play()
        self.assertEqual(played, [("plan", 5), ("go", 5), ("fix", 6), ("plan", 5), ("fix", 6)])

    def test_a_plan_that_asks_after_autopilots_answer_waits(self):
        question = {"question": "Which port?", "options": ["8080", "9090"], "recommended": 1}

        def script(pipe, inputs):
            return plan(questions=[question])

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5), ("plan", 5)])

    def test_parts_of_a_split_plan_are_each_carried_through(self):
        parts = [
            {"title": "First part", "body": "A.", "depends_on": []},
            {"title": "Second part", "body": "B.", "depends_on": [1]},
        ]

        def script(pipe, inputs):
            if inputs["command"] == "plan":
                number = int(inputs["number"])
                return plan(too_large=True, split=parts) if number == 5 else plan()
            return done()

        pipe = Pipeline(self, config(close_parent_issues=True), script)
        pipe.issue(5)
        played = pipe.play()
        self.assertEqual(played[0], ("plan", 5))
        self.assertTrue(all(pipe.gh.issues[n]["state"] == "closed" for n in (5, 6, 7)), played)
        # Without `close_parent_issues`, the parent waits for a person to close it.
        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        pipe.play()
        self.assertEqual(pipe.gh.issues[5]["state"], "open")
        self.assertTrue(pipe.gh.comments_matching(5, "every sub-issue of this parent issue"))


if __name__ == "__main__":
    unittest.main()
