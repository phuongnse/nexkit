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
import contextlib
import copy
import io
import itertools
import json
import os
import re
import sys
import tempfile
import unittest
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from nexkit import autopilot, cli, first_state, host, item_state, progress, state
from nexkit.checks import check_matrix
from nexkit.github import GitHubError
from nexkit.maintain import maintain, paused_runs, within_reach
from nexkit.publish import issue_closed, publish_plan
from nexkit.report import report
from nexkit.route import auto_kinds, issue_from_branch, queue_item, route, skip_unaffected
from nexkit.state import (
    ACTIONS_BOT,
    STATE_PREFIX,
    UNCOUNTED,
    check_fingerprint,
    read_state,
    render_run,
    run_comments,
    with_run,
)
from nexkit.status import collect
from tests.support import BOT_LOGIN, FakeGitHub, make_config
from tests.test_item_state import CREATED, HELD, RELEASED
from tests.test_item_state import step as engine_step

WORKFLOW = "acme/app/.github/workflows/nexkit.yml@refs/heads/main"
# The time at which the first import of an item is read, so that two imports are equal.
IMPORTED_AT = datetime(2026, 1, 3, tzinfo=UTC)
# The jobs of a run in each stage, as GitHub lists them while it runs there.
STAGES = ["route", "agent", "publish", "verify", "review", "report"]
# A time as NexKit writes it, and a marker that holds base64 JSON.
TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
MARKER = re.compile(r"(<!-- nexkit:[a-z-]+ )([A-Za-z0-9+/=]+)( -->)")


def timeless(body):
    """`body` with every ISO time, in its text and in NexKit's base64 JSON markers, replaced:
    two items played in different seconds then read the same."""

    def walk(value):
        if isinstance(value, str):
            return "<time>" if TIME.fullmatch(value) else value
        if isinstance(value, list):
            return [walk(item) for item in value]
        if isinstance(value, dict):
            return {key: walk(item) for key, item in value.items()}
        return value

    def marker(match):
        try:
            data = json.loads(base64.b64decode(match.group(2), validate=True))
        except ValueError:
            return match.group(0)
        text = json.dumps(walk(data), sort_keys=True).encode()
        return f"{match.group(1)}{base64.b64encode(text).decode()}{match.group(3)}"

    return TIME.sub("<time>", MARKER.sub(marker, body))


def people_read(pipe, numbers=(5, 6)):
    """The comments on `numbers` that are not state comments, with times set aside."""
    found = [c["body"] for n in numbers for c in pipe.gh.comments(n)]
    return [timeless(t) for t in found if not t.startswith("### NexKit state")]


def jobs_in(stage):
    jobs = [{"name": f"nexkit / {job}", "status": "completed"} for job in STAGES]
    return [*jobs[: STAGES.index(stage)], {"name": f"nexkit / {stage}", "status": "in_progress"}]


# More dispatches than this in one scenario is a loop. `step` runs count apart: each writing
# issue gets one in every sweep.
MAX_DISPATCHES = 60
MAX_STEPS = 200
TOKENS = {"input": 10, "cached": 0, "output": 10}


def plan(questions=(), too_large=False, split=(), **fields):
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
        "protected_paths": [],
        "no_change": "",
    }
    agent = {"status": "done", "output": output, "tokens": TOKENS}
    for key, value in fields.items():
        (output if key in output else agent)[key] = value
    return {"agent": agent}


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

    def __init__(self, test, cfg, script, workflow=None):
        self.test = test
        self.gh = FakeGitHub()
        self.cfg = cfg
        self.script = script
        # What a dispatch of another workflow, such as CI after a merge, leads to.
        self.workflow = workflow
        self.handled = 0
        self.step_dispatches = 0  # `step` dispatches handled, which MAX_DISPATCHES leaves out
        self.steps = 0  # `step` runs played
        # The places in `gh.dispatches` of the sweeps that a `step` run started after a split.
        self.split_sweeps = set()
        # How many `step` runs GitHub drops from the queue before they run.
        self.lose_steps = 0
        self.runs = itertools.count(100)
        self.heads = itertools.count(1)
        self.played = []  # (command, number, outcome) for each run
        self.reviews = 0  # review agents that ran
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "acme/app"})
        env.start()
        test.addCleanup(env.stop)
        for patched in ("nexkit.report.time.sleep", "nexkit.maintain.time.sleep"):
            sleep = mock.patch(patched)
            sleep.start()
            test.addCleanup(sleep.stop)
        # The sweep job waits for the `step` runs it sent: they run, in the issue's queue.
        waits = mock.patch("nexkit.maintain.wait_for_ends", self.wait_for_ends)
        waits.start()
        test.addCleanup(waits.stop)
        self.gh.add_milestone(1)
        self.dispatch = self.gh.dispatch
        self.gh.dispatch = self.dispatched
        self.merge = self.gh.merge
        self.gh.merge = self.merged
        self.post = self.gh.comment
        self.gh.comment = self.posted
        # `publish` merges a base branch that moved while the agent worked, when the rules
        # want pull requests up to date (`publish.catch_up`); off, it publishes as before.
        self.catch_up = True
        # What may read Dependabot alerts in a sweep, when the App may.
        self.alerts = None
        # How much time `later` moved on.
        self.elapsed = timedelta(0)

    def posted(self, number, body):
        """A comment, posted now: how recent a round's comment is tells whether the round
        it started is still on its way."""
        comment = self.post(number, body)
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        # As GitHub does, a new comment was last updated when it was posted.
        comment["created_at"] = comment["updated_at"] = now
        return comment

    def merged(self, number, sha, method="squash"):
        """GitHub refuses to merge a branch behind its base when the rules want it up to
        date."""
        if self.gh.strict and self.gh.pulls[number].get("mergeable_state") == "behind":
            raise GitHubError(405, "Head branch is out of date. Review and try again.")
        return self.merge(number, sha, method)

    def dispatched(self, workflow, ref, inputs, run_details=False):
        """A dispatch of NexKit's workflow queues a run of its own, which GitHub names."""
        found = self.dispatch(workflow, ref, inputs, run_details)
        if workflow != "nexkit.yml":
            return found
        run_id = next(self.runs)
        self.gh.dispatches[-1]["run_id"] = run_id
        self.gh.runs[run_id] = {"status": "queued"}
        url = f"https://github.com/acme/app/actions/runs/{run_id}"
        return {"workflow_run_id": run_id, "html_url": url} if run_details else None

    def issue(self, number, **kw):
        return self.gh.add_issue(number, title=f"Issue {number}", milestone=1, **kw)

    # -- playing ------------------------------------------------------------------

    def take_run(self, number):
        """The start of a run in item `number`'s group, as `nexkit route` makes it
        (`cli._take`): it stores the item's first state when there is none, then takes its
        events. The import is read at `IMPORTED_AT`. Returns what it printed."""
        real = host.store_import

        def fixed(gh, cfg, item, now=None):
            return real(gh, cfg, item, now=IMPORTED_AT)

        out = io.StringIO()
        with mock.patch.object(host, "store_import", fixed), contextlib.redirect_stdout(out):
            cli._take(self.gh, self.cfg, "workflow_dispatch", {"inputs": {"number": str(number)}})
        return out.getvalue()

    def state_comments(self, number):
        """The state comments of item `number`, which the App posted."""
        return [c for c in self.gh.comments(number) if c["body"].startswith("### NexKit state")]

    def play(self):
        """Run every dispatch, and the ones they start, then the hourly sweeps that time
        brings, until nothing changes. Returns the commands that ran."""
        autopilot.request(self.gh, "nexkit.yml", "main")
        self.drain()
        for _ in range(6):
            # Seven hours each time: what only time moves on, such as a lost command.
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
            self.drain_one()

    def drain_one(self):
        """Run the next dispatch."""
        dispatch = self.gh.dispatches[self.handled]
        inputs = dispatch["inputs"]
        self.handled += 1
        if dispatch["workflow"] == "nexkit.yml" and inputs["command"] == "step":
            self.step_dispatches += 1
            self.play_step(dispatch)
            return
        handled = self.handled - self.step_dispatches
        self.test.assertLess(handled, MAX_DISPATCHES, f"a loop: {self.played}")
        if dispatch["workflow"] != "nexkit.yml":
            if self.workflow:
                self.workflow(self, dispatch["workflow"])
        elif inputs["command"] == "resume":
            self.waited(inputs)
        elif inputs["command"] == "autopilot":
            decision = route(self.gh, "workflow_dispatch", {}, inputs)
            maintain(
                self.gh,
                decision,
                self.cfg,
                workflow="nexkit.yml",
                ref="main",
                alerts=self.alerts,
            )
            self.gh.runs[dispatch["run_id"]] = {"status": "completed"}
            self.check()
        else:
            self.run(dispatch)

    def play_step(self, dispatch):
        """A `step` run, in the issue's queue: it runs the issue's step, unless a sweep's wait
        played it already, or GitHub dropped it (`lose_steps`)."""
        run_id = dispatch["run_id"]
        if self.gh.runs[run_id]["status"] == "completed":
            return
        if self.lose_steps:
            self.lose_steps -= 1
            self.gh.runs[run_id] = {"status": "completed", "conclusion": "cancelled"}
            return
        self.steps += 1
        self.test.assertLess(self.steps, MAX_STEPS, f"a loop: {self.played}")
        before = len(self.gh.dispatches)
        decision = route(self.gh, "workflow_dispatch", {}, dispatch["inputs"])
        if decision["action"] == "none":
            # As `nexkit route` ends it: a step with nothing to do, such as an approval of a
            # commit that is no longer the head.
            self.gh.runs[run_id] = {"status": "completed"}
            return
        lines = maintain(
            self.gh, decision, self.cfg, workflow="nexkit.yml", ref="main", alerts=self.alerts
        )
        # A split that GitHub refused in part still starts the sweep.
        if any(line.startswith(("Split #", "Could not split #")) for line in lines):
            self.split_sweeps.update(
                place
                for place in range(before, len(self.gh.dispatches))
                if self.gh.dispatches[place]["inputs"].get("command") == "autopilot"
            )
        self.gh.runs[run_id] = {"status": "completed"}

    def wait_for_ends(self, gh, run_ids, tries=0, delay=0):
        """The sweep job's wait for its `step` runs: they run while it waits."""
        for dispatch in self.gh.dispatches:
            if dispatch.get("run_id") in run_ids:
                self.play_step(dispatch)
        return f"The {len(run_ids)} step runs ended."

    def sweep(self):
        """A sweep and its `step` runs, in the order the sweep job plays them."""
        sent = []
        lines = autopilot.sweep(self.gh, self.cfg, "nexkit.yml", "main", sent=sent)
        return lines + [self.wait_for_ends(self.gh, sent)]

    def approve(self, commit=None, login="olivia", at=None):
        """A writer's approval of pull request 6 on `commit` (its head by default): the review
        GitHub holds, submitted at `at` when given, and the run of the approval, which sends a
        `step` into the issue's queue. Returns the lines of that run."""
        pull = self.gh.pulls[6]
        review = {
            "id": 9000 + len(self.gh.pr_reviews[6]),
            "state": "APPROVED",
            "user": {"login": login},
            "commit_id": commit or pull["head"]["sha"],
        }
        if at:
            review["submitted_at"] = at
        self.gh.pr_reviews[6].append(review)
        event = {
            "action": "submitted",
            "review": {**review, "state": "approved"},
            "pull_request": {**pull, "base": {"ref": "main", "repo": {"full_name": "acme/app"}}},
        }
        decision = route(self.gh, "pull_request_review", event)
        return maintain(self.gh, decision, self.cfg, workflow="nexkit.yml", ref="main")

    def waited(self, inputs):
        """The run that waits for paused commands: time passes until the first one within
        its reach comes due, then it starts what is due."""
        now = datetime.now(UTC)
        due = [
            at
            for _, _, run, at in paused_runs(self.gh, now, self.cfg)
            if within_reach(self.cfg, run, at, now)
        ]
        if due and min(due) > now:
            self.later(min(due) - now)
        decision = route(self.gh, "workflow_dispatch", {}, inputs)
        maintain(self.gh, decision, self.cfg, workflow="nexkit.yml", ref="main")

    def run(self, dispatch):
        inputs, run_id = dispatch["inputs"], dispatch["run_id"]
        outcome = self.script(self, inputs)
        if outcome == "lost":
            # GitHub dropped it from the queue: its run never started, and no comment.
            self.gh.runs[run_id] = {"status": "completed", "conclusion": "cancelled"}
            self.played.append((inputs["command"], int(inputs["number"]), "lost"))
            return
        os.environ["GITHUB_RUN_ID"] = str(run_id)
        self.gh.runs[run_id] = {"status": "in_progress"}
        decision = route(
            self.gh, "workflow_dispatch", {"sender": {"login": BOT_LOGIN}}, inputs, cfg=self.cfg
        )
        # As `nexkit route` does once it read the configuration.
        decision = skip_unaffected(self.gh, decision, self.cfg)
        if outcome == "refused":
            decision = {"action": "none", "reason": "refused"}
        if decision["action"] == "none":
            # `route` refused it. For a command NexKit started by itself, the run starts a
            # sweep that waits for it to end (`cli._refused_dispatch`).
            if auto_kinds(inputs.get("auto")) - {"false"}:
                lost = {"action": "maintain", "task": "lost", "after": run_id}
                maintain(self.gh, lost, self.cfg, workflow="nexkit.yml", ref="main")
            self.gh.runs[run_id] = {"status": "completed"}
            self.played.append((inputs["command"], int(inputs["number"]), decision["reason"]))
            return
        if decision["action"] == "maintain":
            # `route` handed the command to the `maintain` job, such as for a pull request
            # whose base branch already holds its change; no agent runs.
            maintain(self.gh, decision, self.cfg, workflow="nexkit.yml", ref="main")
            self.gh.runs[run_id] = {"status": "completed"}
            self.played.append((inputs["command"], int(inputs["number"]), decision["task"]))
            return
        progress.start(self.gh, decision)
        if outcome == "stall":
            # The runner was lost: report never ran, and the run's `finish` job starts a
            # sweep that waits for the run to end.
            autopilot.after_unreported(self.gh, self.cfg, "nexkit.yml", "main", run_id)
            self.gh.runs[run_id] = {"status": "completed", "conclusion": "failure"}
            self.played.append((inputs["command"], int(inputs["number"]), "stall"))
            return
        artifacts = Path(self.tmp.name) / str(run_id)
        needs = self.stages(decision, outcome, artifacts)
        if run_id in self.gh.cancelled:
            # NexKit stopped the run in its checks: no `report` or `finish` job runs.
            self.played.append((inputs["command"], decision["target"], "superseded"))
            return
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

        start = self.gh.branch_sha("main")
        run_id = int(os.environ["GITHUB_RUN_ID"])
        self.gh.jobs[run_id] = jobs_in("agent")
        if outcome.get("working"):
            # What happens elsewhere while the agent works, before anything is published.
            outcome["working"](self)
        if outcome.get("meanwhile") and decision["action"] != "implement":
            # What happens elsewhere while the round runs on its pull request.
            outcome["meanwhile"](self, decision["pr"])
        agent = outcome.get("agent")
        if agent is not None and decision["action"] != "review":
            written = {"stage": decision["action"], **agent}
            if decision["action"] in ("implement", "fix") and agent["status"] == "done":
                # What the agent job leaves after it checked its output against the stage's
                # schema (`results/<stage>.schema.json`), which `report` stores for the engine.
                written.setdefault(
                    "output",
                    {
                        "status": "done",
                        "summary": agent.get("summary", ""),
                        "checks_run": [],
                        "changes": [],
                        "testing": "",
                    },
                )
            write("nexkit-agent", written)
        needs = {}
        published = {"published": False}
        issue_round = decision["action"] in ("plan", "implement")
        if issue_round and agent and agent["status"] == "done":
            closed = issue_closed(self.gh, decision["issue"])
        else:
            closed = False
        if closed:
            published = {"published": False, "issue_closed": True}
        elif decision["action"] == "plan" and agent and agent["status"] == "done":
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
        tip = self.gh.branch_sha("main")
        if published.get("head") and tip != start and not published.get("conflicts"):
            # The base branch moved while the agent worked, without a conflict.
            if self.gh.strict and self.catch_up:
                published["updated"] = tip
            elif self.gh.strict:
                self.gh.pulls[published["pr"]]["mergeable_state"] = "behind"
        needs["publish"] = {"outputs": {"result": json.dumps(published)}}
        # A head that conflicts with the base branch's tip gets no checks or review.
        reviewing = decision["action"] == "review" or (
            published.get("head") and not published.get("conflicts")
        )
        if reviewing and outcome.get("checking"):
            # What happens elsewhere while the round runs its checks.
            self.gh.jobs[run_id] = jobs_in("verify")
            outcome["checking"](self, published.get("pr") or decision["pr"])
            if run_id in self.gh.cancelled:
                return needs
        self.gh.jobs[run_id] = jobs_in("review")
        if reviewing and "checks" in outcome:
            write("nexkit-checks", outcome["checks"])
        if decision.get("carry_from"):
            # The review job compares the change with the approved one before any agent.
            carried = not outcome.get("changed")
            record = {"from": decision["carry_from"], "carried": carried, "why": ""}
            if not carried:
                record["why"] = "the change against `main` differs from the one the review approved"
            (artifacts / "nexkit-review").mkdir(parents=True, exist_ok=True)
            (artifacts / "nexkit-review" / "carry.json").write_text(json.dumps(record))
            if carried:
                write(
                    "nexkit-review",
                    {"stage": "review", "status": "carried", "carried": record["from"]},
                )
                return needs
        if reviewing and "review" in outcome:
            self.reviews += 1
            write("nexkit-review", outcome["review"])
        return needs

    def head(self, pull):
        sha = f"{next(self.heads):040x}"
        self.gh.pulls[pull]["head"]["sha"] = sha
        return sha

    def later(self, step):
        """Move every record and comment of NexKit, and the time each issue closed, back by
        `step`, as if that much time passed."""
        self.elapsed += step
        for issue in self.gh.issues.values():
            if issue.get("closed_at"):
                at = datetime.fromisoformat(issue["closed_at"].replace("Z", "+00:00"))
                issue["closed_at"] = (at - step).strftime("%Y-%m-%dT%H:%M:%SZ")
        for comments in self.gh.issue_comments.values():
            for comment in comments:
                for key in ("created_at", "updated_at"):
                    at = datetime.fromisoformat(
                        (comment.get(key) or "2026-01-01T00:00:00Z").replace("Z", "+00:00")
                    )
                    comment[key] = (at - step).strftime("%Y-%m-%dT%H:%M:%SZ")
                found = next(run_comments([comment]), None)
                resume = found[1].get("resume") if found else None
                if isinstance(resume, dict) and resume.get("at"):
                    # When a paused run resumes, or one runs again after a transient error.
                    at = datetime.fromisoformat(resume["at"].replace("Z", "+00:00")) - step
                    resume["at"] = autopilot.iso(at)
                    comment["body"] = with_run(comment["body"], found[1])
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
        people = {s["number"] for s in steps if s["state"] == "waits" and s.get("person")}
        # A `step` run that splits an issue starts a sweep, which sends the mention.
        sweeping = any(place >= self.handled for place in self.split_sweeps)
        for step in steps:
            where = f"#{step['number']} {step['state']}: {step['reason']} after {self.played}"
            if step["state"] == "waits" and step.get("person"):
                numbers = {step["number"], step.get("pull")}
                self.test.assertTrue(
                    any((n, step["reason"]) in listed for n in numbers), f"not listed: {where}"
                )
                if self.cfg["notify"] and not sweeping:
                    self.test.assertTrue(step.get("mentioned"), f"nobody mentioned: {where}")
            if quiet:
                # Or it waits for an issue that waits for a person, who hears about that one.
                awaited = {int(n) for n in re.findall(r"#(\d+)", step["reason"])}
                # A label that holds an issue is a person's own choice.
                self.test.assertTrue(
                    step["state"] == "held"
                    or (step["state"] == "waits" and (step.get("person") or awaited & people)),
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
            if d["inputs"].get("command") == "fix"
            and d["inputs"]["auto"] == "true"
            and d["inputs"]["number"] == str(pull)
        )
        self.test.assertLessEqual(state["auto_fixes"], self.cfg["fix"]["max_auto_rounds"])
        self.test.assertEqual(state["auto_fixes"], len(ran) + waiting, f"#{pull}: {self.played}")


class ItemGroup:
    """One work item's concurrency group, as GitHub keeps it: one run runs and one waits, and a
    run queued while another waits replaces it, so its event is never answered by its own run.
    Each run takes the item's events from their sources as `nexkit route` does at its start
    (`cli._take`, docs/engine-hosts.md#hosts), on a state comment that is already stored, or,
    with `stored` off, with none, so that the first run stores the item's import. With `routes`,
    a run that is queued with the comment of its event also routes it (`route`) on what it took,
    as `nexkit route` does, and `decisions` holds each run's decision."""

    NUMBER = 5

    def __init__(self, test, stored=True, routes=False):
        self.test = test
        self.routes = routes
        self.comments = {}  # the comment that gave a run its event
        self.decisions = {}  # what each run that routed decided
        self.gh = FakeGitHub()
        self.cfg = make_config(autopilot={"enabled": True})
        self.gh.add_issue(self.NUMBER)
        if stored:
            first, _, _ = engine_step(None, {**CREATED, "item": str(self.NUMBER)})
            test.assertTrue(item_state.write(self.gh, self.NUMBER, first, (None, None)))
        self.running = None
        self.waiting = None
        self.replaced = []  # the runs that a newer one replaced, which never ran
        self.failed = []  # the runs that failed because every write lost
        self.answered = []  # the id of each event that a run sent to the engine, in order
        self.clock = itertools.count(1)

    def post(self, body, login="alice"):
        """A person's comment on the item, one second after the one before."""
        at = f"2026-01-02T00:00:{next(self.clock):02d}Z"
        return self.gh.human_comment(self.NUMBER, body, login=login, at=at)

    def queue(self, run, comment=None):
        """A run of the item's group, whose event is the posted `comment`, starts when none
        runs, and otherwise waits, replacing the one that waits."""
        if comment is not None:
            self.comments[run] = comment
        if self.running is None:
            self._start(run)
            return
        if self.waiting is not None:
            self.replaced.append(self.waiting)
        self.waiting = run

    def finish(self):
        """The run that runs ends, and the one that waits starts."""
        self.running, run, self.waiting = None, self.waiting, None
        if run is not None:
            self._start(run)

    def _start(self, run):
        """A run starts by taking the item's events, and fails when every try to store an
        answer lost."""
        self.running = run
        real, take = host.step, host.take

        def spy(state, event, found):
            self.answered.append(event["id"])
            return real(state, event, found)

        def quick(gh, cfg, number):
            return take(gh, cfg, number, sleep=lambda seconds: None)

        # The event of a run in the item's group: a comment on the item.
        comment = self.comments.get(run) if self.routes else None
        event = {
            "action": "created",
            "issue": {"number": self.NUMBER},
            "comment": comment or {"id": 1},
        }
        with (
            mock.patch.object(host, "step", spy),
            mock.patch.object(host, "take", quick),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            try:
                taken = cli._take(self.gh, self.cfg, "issue_comment", event)
                if comment is not None:
                    self.decisions[run] = route(
                        self.gh, "issue_comment", event, cfg=self.cfg, taken=taken
                    )
            except item_state.Moved:
                self.failed.append(run)
                self.finish()

    def state(self):
        return item_state.read(self.gh, self.NUMBER)[1]

    def cursor(self):
        return item_state.since(item_state.read(self.gh, self.NUMBER)[0])

    def losing(self):
        """Every write of a run loses: another job stores its answer to a hold, as the
        group's other writers would, between a write's read and its edit."""
        return mock.patch.object(self.gh, "issue_comment", self._other_first(self.gh.issue_comment))

    def _other_first(self, real):
        busy = []

        def read(comment_id):
            if not busy:
                busy.append(True)
                found = item_state.read(self.gh, self.NUMBER)
                other = {**(RELEASED if found[1]["hold"] else HELD), "item": str(self.NUMBER)}
                other["id"] = f"other-{found[1]['revision']}"
                new, _, _ = engine_step(found[1], other)
                self.test.assertTrue(item_state.write(self.gh, self.NUMBER, new, found))
                busy.clear()
            return real(comment_id)

        return read


def review_state(gh, head):
    """The state of `nexkit/review` on `head`."""
    return {s["context"]: s for s in gh.commit_statuses(head)}["nexkit/review"]["state"]


def red_main(gh):
    """`ci.yml` failed on `main` twice, in the step that runs `make test`, where its test
    reporter names the failing test."""
    gh.workflow_history["ci.yml"] = [
        {
            "id": 50,
            "head_branch": "main",
            "head_repository": {"full_name": "acme/app"},
            "event": "push",
            "head_sha": "e" * 40,
            "status": "completed",
            "conclusion": "failure",
            "run_attempt": 2,
            "html_url": "https://github.com/acme/app/actions/runs/50",
            "created_at": autopilot.iso(datetime.now(autopilot.UTC)),
        }
    ]
    gh.jobs[50] = [{"id": 501, "name": "Unit tests", "conclusion": "failure"}]
    gh.logs[501] = (
        b"##[group]Run make test\n##[endgroup]\nFAIL\n"
        b"##[error]Process completed with exit code 1.\n"
    )
    gh.annotations[501] = [
        {
            "annotation_level": "failure",
            "title": "Orders > adds tax",
            "path": "web/src/orders.test.ts",
            "message": "expected 2 to be 3",
        }
    ]


def alert(number=1, ghsa="GHSA-aaaa-bbbb-cccc", severity="high", package="lodash"):
    """A Dependabot alert, as GitHub lists it."""
    return {
        "number": number,
        "html_url": f"https://github.com/acme/app/security/dependabot/{number}",
        "dependency": {
            "package": {"ecosystem": "npm", "name": package},
            "manifest_path": "web/package-lock.json",
        },
        "security_advisory": {
            "ghsa_id": ghsa,
            "cve_id": "CVE-2026-1",
            "summary": "Prototype pollution",
            "severity": severity,
        },
        "security_vulnerability": {
            "vulnerable_version_range": "< 4.17.21",
            "first_patched_version": {"identifier": "4.17.21"},
        },
    }


def issues(pipe):
    """The numbers of the issues, without pull requests."""
    return sorted(n for n, issue in pipe.gh.issues.items() if "pull_request" not in issue)


def config(**kw):
    autopilot_settings = {"enabled": True, "answer": "recommended", **kw.pop("autopilot", {})}
    return make_config(
        autopilot=autopilot_settings,
        merge={"auto": True, **kw.pop("merge", {})},
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

    def test_a_sweep_and_its_step_runs_take_a_plan_that_needs_nobody_to_its_merge(self):
        def script(pipe, inputs):
            return plan() if inputs["command"] == "plan" else done()

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5)])
        self.assertEqual(pipe.gh.issues[5]["state"], "closed")
        self.assertEqual(pipe.gh.merged, [(6, pipe.gh.pulls[6]["head"]["sha"], "squash")])
        # The sweeps wrote none of it: each start and the close came from a `step` run.
        steps = [d["inputs"] for d in pipe.gh.dispatches if d["inputs"]["command"] == "step"]
        self.assertTrue(steps)
        self.assertEqual({step["number"] for step in steps}, {"5"})
        self.assertTrue(pipe.gh.comments_matching(5, "NexKit autopilot finished milestone M1"))

    def test_a_step_dispatch_that_never_ran_is_sent_again_by_a_later_sweep(self):
        def script(pipe, inputs):
            return plan() if inputs["command"] == "plan" else done()

        pipe = Pipeline(self, config(), script)
        pipe.lose_steps = 1
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5)])
        steps = [d for d in pipe.gh.dispatches if d["inputs"]["command"] == "step"]
        lost, *later = steps
        self.assertEqual(pipe.gh.runs[lost["run_id"]]["conclusion"], "cancelled")
        # Nothing on the issue changed, so the next sweep found the same step and sent it.
        self.assertEqual(lost["inputs"], later[0]["inputs"])
        self.assertEqual(pipe.gh.issues[5]["state"], "closed")

    def test_a_round_that_blocks_after_autopilots_answer_waits(self):
        # Every go blocks, recommending that a person download the artifacts.
        def script(pipe, inputs):
            return plan() if inputs["command"] == "plan" else blocked()

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5), ("go", 5)])
        self.assertEqual(len(pipe.gh.comments_matching(5, "answers for the owner")), 1)
        self.assertEqual(len(pipe.gh.comments_matching(5, "@olivia a person is needed")), 1)

    def test_an_issue_of_a_trusted_workflow_runs_like_a_persons(self):
        def script(pipe, inputs):
            return plan() if inputs["command"] == "plan" else done()

        cfg = config(autopilot={"trusted_workflows": ["audit.yml"]})
        pipe = Pipeline(self, cfg, script)
        pipe.gh.runs[900] = {
            "id": 900,
            "path": ".github/workflows/audit.yml",
            "head_branch": "main",
            "head_repository": {"full_name": "acme/app"},
            "event": "schedule",
            "status": "completed",
            "created_at": "2026-11-01T10:00:00Z",
            "updated_at": "2026-11-01T10:10:00Z",
        }
        body = "<!-- nexkit:workflow audit.yml 900 -->\nStale docs."
        pipe.issue(5, body=body, user=ACTIONS_BOT)["created_at"] = "2026-11-01T10:05:00Z"
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5)])
        self.assertEqual(pipe.gh.issues[5]["state"], "closed")

    def test_an_issue_whose_marker_fails_waits_for_a_person_and_says_why(self):
        cfg = config(autopilot={"trusted_workflows": ["audit.yml"]})
        pipe = Pipeline(self, cfg, lambda pipe, inputs: plan())
        body = "<!-- nexkit:workflow audit.yml 404 -->\nStale docs."
        pipe.issue(5, body=body, user=ACTIONS_BOT)
        self.assertEqual(pipe.play(), [])
        [mention] = pipe.gh.comments_matching(5, "@olivia a person is needed")
        self.assertIn("its marker names run 404, which does not exist", mention["body"])

    def test_a_part_of_an_earlier_release_runs_once_a_writer_vouches_for_it(self):
        def script(pipe, inputs):
            return plan() if inputs["command"] == "plan" else done()

        pipe = Pipeline(self, config(), script)
        pipe.issue(5, body="<!-- nexkit:part 4 -->\nThe part.", user=ACTIONS_BOT)
        self.assertEqual(pipe.play(), [])
        [mention] = pipe.gh.comments_matching(5, "@olivia a person is needed")
        self.assertIn("comment `/nexkit trust` on it", mention["body"])
        # Someone without write access changes nothing, and nobody is mentioned again.
        pipe.gh.human_comment(5, "/nexkit trust", "bob", "NONE")
        self.assertEqual(pipe.play(), [])
        self.assertEqual(len(pipe.gh.comments_matching(5, "@olivia a person is needed")), 1)
        comment = pipe.gh.human_comment(5, "/nexkit trust", "olivia", "OWNER")
        comment["created_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        decision = {"task": "trust", "issue": 5, "actor": "olivia", "comment_id": comment["id"]}
        maintain(pipe.gh, decision, pipe.cfg, workflow="nexkit.yml", ref="main")
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5)])
        self.assertEqual(pipe.gh.issues[5]["state"], "closed")

    def test_an_edit_after_the_vouch_stops_the_issue_until_a_writer_vouches_again(self):
        def script(pipe, inputs):
            if inputs["command"] == "plan":
                # While the plan runs, someone without write access edits the issue.
                edited = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
                pipe.gh.edits[5] = [("bob", edited)]
                return plan()
            return done()

        pipe = Pipeline(self, config(), script)
        pipe.issue(5, user="bob")
        pipe.gh.human_comment(5, "/nexkit trust", "olivia", "OWNER")
        self.assertEqual(pipe.play(), [("plan", 5)])
        self.assertEqual(pipe.gh.issues[5]["state"], "open")
        [mention] = pipe.gh.comments_matching(5, "@olivia a person is needed")
        # It names who edited it in a code span, which mentions nobody.
        self.assertIn("`@bob` edited it without write access", mention["body"])

    def test_an_advisory_is_planned_fixed_and_merged_once(self):
        def script(pipe, inputs):
            return plan() if inputs["command"] == "plan" else done()

        pipe = Pipeline(self, config(autopilot={"dependabot": "high"}), script)
        pipe.alerts = pipe.gh
        pipe.gh.alerts = [alert()]
        pipe.issue(4)
        played = pipe.play()
        # In the milestone scope too, though it has no milestone, and before other issues.
        self.assertEqual(played, [("plan", 5), ("plan", 4), ("go", 5), ("go", 4)])
        self.assertEqual(pipe.gh.issues[5]["state"], "closed")
        self.assertIsNone(pipe.gh.issues[5]["milestone"])

    def test_an_alert_that_outlives_its_issue_starts_no_more_work(self):
        def script(pipe, inputs):
            return plan() if inputs["command"] == "plan" else done()

        pipe = Pipeline(self, config(autopilot={"dependabot": "high"}), script)
        pipe.alerts = pipe.gh
        pipe.gh.alerts = [alert()]
        # The alert stays open after the merge: more sweeps open no second issue.
        played = pipe.play()
        self.assertEqual(played, [("plan", 1), ("go", 1)])
        pipe.later(timedelta(days=2))
        self.assertEqual(pipe.play(), played)
        self.assertEqual(len(pipe.gh.issues), 2)  # the issue and its pull request
        [word] = pipe.gh.comments_matching(1, autopilot.DEPENDABOT_STILL_OPEN)
        self.assertIn("@olivia", word["body"])
        pipe.later(timedelta(days=2))
        pipe.play()
        self.assertEqual(len(pipe.gh.comments_matching(1, autopilot.DEPENDABOT_STILL_OPEN)), 1)

    def test_the_parts_of_an_advisory_run_in_the_milestone_scope(self):
        parts = [{"title": "First part", "body": "A."}, {"title": "Second part", "body": "B."}]

        def script(pipe, inputs):
            if inputs["command"] == "plan" and int(inputs["number"]) == 1:
                return plan(too_large=True, split=parts)
            return plan() if inputs["command"] == "plan" else done()

        cfg = config(autopilot={"dependabot": "high"}, close_parent_issues=True)
        pipe = Pipeline(self, cfg, script)
        pipe.alerts = pipe.gh
        pipe.gh.alerts = [alert()]
        played = pipe.play()
        self.assertIn(("go", 2), played)
        self.assertIn(("go", 3), played)
        self.assertEqual(pipe.gh.issues[1]["state"], "closed")

    def test_an_edit_while_a_round_runs_stops_it_until_a_writer_vouches_again(self):
        def at(days):
            return (datetime.now(UTC) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return plan()
            if not pipe.gh.edits.get(5):
                # The author edits the issue just after this `/nexkit go` started.
                pipe.gh.edits[5] = [("bob", at(1))]
            if autopilot.edited_after_vouch(pipe.gh, pipe.gh.issues[5]):
                # What `context.gather` and the agent job make of it.
                return {"agent": {"status": "untrusted", "error": "@bob edited it"}}
            return done()

        pipe = Pipeline(self, config(), script)
        pipe.issue(5, user="bob")
        pipe.gh.human_comment(5, "/nexkit trust", "olivia", "OWNER")
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5)])
        self.assertEqual(pipe.played[-1][2], "agent_untrusted")
        self.assertEqual(pipe.gh.pulls, {})
        comment = pipe.gh.human_comment(5, "/nexkit trust", "olivia", "OWNER")
        comment["created_at"] = at(2)
        played = pipe.play()
        self.assertEqual(played[2:], [("plan", 5), ("go", 5)])
        self.assertEqual(pipe.gh.issues[5]["state"], "closed")

    def test_an_app_that_may_not_read_alerts_opens_nothing_and_the_other_work_goes_on(self):
        def script(pipe, inputs):
            return plan() if inputs["command"] == "plan" else done()

        pipe = Pipeline(self, config(autopilot={"dependabot": "high"}), script)
        pipe.gh.alerts = [alert()]
        pipe.issue(4)
        self.assertEqual(pipe.play(), [("plan", 4), ("go", 4)])
        self.assertEqual(sorted(pipe.gh.issues), [4, 5])  # the issue and its pull request

    def persons_pull_request(self, pipe):
        pull = pipe.gh.add_pull(9, 0, branch="upgrade/nexkit")
        pipe.head(9)
        inputs = {"command": "review", "number": "9", "note": "", "auto": "false"}
        pipe.gh.dispatch("nexkit.yml", "main", inputs)
        return pull

    def test_a_writers_review_of_a_persons_pull_request_changes_nothing_on_it(self):
        for checks, verdict in ((True, "approve"), (False, "request_changes")):
            outcome = done(checks, verdict)
            pipe = Pipeline(self, config(), lambda pipe, inputs, found=outcome: found)
            pull = self.persons_pull_request(pipe)
            head = pull["head"]["sha"]
            pipe.play()
            self.assertEqual(pipe.played, [("review", 9, "person_reviewed")])
            self.assertEqual(pipe.reviews, 1)
            self.assertEqual(pipe.gh.merged, [])
            self.assertEqual((pull["state"], pull["head"]["sha"]), ("open", head))
            states = {
                s["context"]: s["state"]
                for s in pipe.gh.statuses
                if s["sha"] == head and s["context"].startswith("nexkit/")
            }
            good = "success" if checks else "failure"
            self.assertEqual(states["nexkit/checks"], good)
            self.assertEqual(states["nexkit/review"], good)

    def test_a_base_branch_move_starts_no_round_on_a_persons_pull_request(self):
        pipe = Pipeline(self, config(), lambda pipe, inputs: done())
        pull = self.persons_pull_request(pipe)
        pipe.play()
        pull["mergeable"] = False
        pipe.play()
        self.assertEqual(pipe.played, [("review", 9, "person_reviewed")])

    def test_an_issue_marked_to_run_last_runs_after_the_rest_of_its_milestone(self):
        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return plan()
            if inputs["number"] == "6":
                # Opened while the milestone runs, with no mark on either issue.
                pipe.issue(8)
            return done()

        pipe = Pipeline(self, config(autopilot={"labels": {"last": {"wave": 1}}}), script)
        pipe.issue(5, labels=("last",))
        pipe.issue(6)
        played = pipe.play()
        self.assertLess(played.index(("go", 8)), played.index(("plan", 5)))
        self.assertEqual(played[-2:], [("plan", 5), ("go", 5)])
        self.assertEqual(pipe.gh.issues[5]["state"], "closed")

    def test_the_parts_of_a_split_hold_an_issue_marked_to_run_last_back(self):
        parts = [
            {"title": "One", "body": "Do one.", "depends_on": []},
            {"title": "Two", "body": "Do two.", "depends_on": []},
        ]

        def script(pipe, inputs):
            if inputs["command"] != "plan":
                return done()
            big = inputs["number"] == "6"
            return plan(too_large=big, split=parts if big else [])

        settings = {"labels": {"last": {"wave": 1}}}
        pipe = Pipeline(self, config(autopilot=settings, close_parent_issues=True), script)
        pipe.issue(5, labels=("last",))
        pipe.issue(6)
        played = pipe.play()
        self.assertEqual(played[-2:], [("plan", 5), ("go", 5)])
        self.assertEqual(len(played), 7)  # #6 plans, each part plans and goes, then #5

    def test_an_issue_that_runs_last_waits_for_a_person_with_the_rest(self):
        def script(pipe, inputs):
            if inputs["number"] == "6":
                return plan(questions=[{"question": "Which?", "options": ["a", "b"]}])
            return plan() if inputs["command"] == "plan" else done()

        pipe = Pipeline(self, config(autopilot={"labels": {"last": {"wave": 1}}}), script)
        pipe.issue(5, labels=("last",))
        pipe.issue(6)
        self.assertEqual(pipe.play(), [("plan", 6)])
        self.assertFalse(pipe.gh.comments_matching(5, "a person is needed"))
        self.assertTrue(pipe.gh.comments_matching(6, "a person is needed"))

    def test_a_checklist_item_not_met_leads_to_a_fix_round(self):
        def reviewed(answer):
            found = done()
            entry = {"item": "Docs change with the code", "answer": answer, "evidence": "x:1"}
            found["review"]["output"]["checklist"] = [entry]
            return found

        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return plan()
            return reviewed("not_met" if inputs["command"] == "go" else "met")

        pipe = Pipeline(self, config(review={"checklist": ["Docs change with the code"]}), script)
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5), ("fix", 6)])
        self.assertEqual(pipe.gh.issues[5]["state"], "closed")
        self.assertEqual(read_state(pipe.gh.comments(6))[1]["auto_fixes"], 1)

    def test_a_checklist_item_a_person_decides_waits_for_their_approval_then_merges(self):
        accepted = "The change adds no exception to a check and loosens none"

        def reviewed(*answers, verdict="request_changes"):
            found = done()
            found["review"]["output"]["verdict"] = verdict
            found["review"]["output"]["checklist"] = [
                {"number": number, "answer": answer, "evidence": "scan.json:3", "item": item}
                | ({"person": True} if item == accepted else {})
                for number, (item, answer) in enumerate(answers, 1)
            ]
            return found

        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return plan()
            if inputs["command"] == "go":
                # Another item fails too: the fix round is for that one alone.
                return reviewed((accepted, "not_met"), ("Docs change with the code", "not_met"))
            return reviewed((accepted, "not_met"), ("Docs change with the code", "met"))

        checklist = [{"text": accepted, "person": True}, "Docs change with the code"]
        pipe = Pipeline(self, config(review={"checklist": checklist}), script)
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5), ("fix", 6)])
        self.assertEqual(pipe.played[-1][2], "ready")
        self.assertEqual(pipe.gh.issues[5]["state"], "open")
        self.assertEqual(pipe.gh.merged, [])
        self.assertEqual(read_state(pipe.gh.comments(6))[1]["auto_fixes"], 1)
        head = pipe.gh.pulls[6]["head"]["sha"]
        self.assertEqual(review_state(pipe.gh, head), "failure")
        [mention] = pipe.gh.comments_matching(6, "a person is needed")
        self.assertIn("decide the checklist items", mention["body"])
        self.assertNotIn("remove its cause", mention["body"])
        [round_] = [run for _, run in run_comments(pipe.gh.comments(6)) if "round" in run][-1:]
        self.assertEqual(round_["decide"], [accepted])

        # An approval of an older head accepts nothing.
        sent = len(pipe.gh.dispatches)
        self.assertIn("it starts nothing", pipe.approve("0" * 40)[0])
        self.assertEqual(len(pipe.gh.dispatches), sent)
        self.assertEqual(pipe.gh.merged, [])
        self.assertEqual(review_state(pipe.gh, head), "failure")
        # The approval of the head only sends a step, which accepts and merges in the queue.
        self.assertEqual(pipe.approve(head), ["#6: sent @olivia's approval to #5's queue."])
        self.assertEqual(review_state(pipe.gh, head), "failure")
        self.assertEqual(pipe.gh.merged, [])
        pipe.drain()
        self.assertEqual(pipe.gh.merged, [(6, head, "squash")])
        self.assertEqual(review_state(pipe.gh, head), "success")
        pipe.play()
        self.assertEqual(pipe.gh.issues[5]["state"], "closed")
        self.assertEqual(len(pipe.gh.comments_matching(6, "a person is needed")), 1)

    def test_a_checklist_item_a_person_decides_alone_starts_no_fix_round(self):
        accepted = "The change adds no exception to a check and loosens none"

        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return plan()
            found = done(verdict="request_changes")
            found["review"]["output"]["findings"] = []
            entry = {"number": 1, "answer": "not_met", "evidence": "x", "item": accepted}
            found["review"]["output"]["checklist"] = [{**entry, "person": True}]
            return found

        checklist = [{"text": accepted, "person": True}]
        pipe = Pipeline(self, config(review={"checklist": checklist}), script)
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5)])
        self.assertEqual(pipe.played[-1][2], "ready")
        self.assertEqual(read_state(pipe.gh.comments(6))[1]["auto_fixes"], 0)
        self.assertEqual(len(pipe.gh.comments_matching(6, "a person is needed")), 1)
        needs = collect(pipe.gh, pipe.cfg, workers=1)["needs_person"]
        self.assertTrue(any("Only a person decides" in item["reason"] for item in needs), needs)

    def test_two_issues_that_run_last_and_a_split_never_wait_in_a_circle(self):
        parts = [{"title": "One", "body": "Do one.", "depends_on": []}]

        def script(pipe, inputs):
            if inputs["command"] != "plan":
                return done()
            big = inputs["number"] == "5"
            return plan(too_large=big, split=parts if big else [])

        settings = {"labels": {"last": {"wave": 1}}}
        pipe = Pipeline(self, config(autopilot=settings, close_parent_issues=True), script)
        pipe.issue(5, labels=("last",))
        pipe.issue(6, labels=("last",))
        played = pipe.play()
        # One wave runs side by side: #5 splits into #7, which runs beside #6.
        self.assertEqual(
            sorted(played), [("go", 6), ("go", 7), ("plan", 5), ("plan", 6), ("plan", 7)]
        )
        self.assertTrue(all(pipe.gh.issues[n]["state"] == "closed" for n in (5, 6, 7)))

    def test_a_persons_bug_issue_runs_before_a_ready_milestone_issue(self):
        def script(pipe, inputs):
            return plan() if inputs["command"] == "plan" else done()

        priority = ["nexkit:ci", "nexkit:dependabot", "bug", "nexkit:flaky"]
        pipe = Pipeline(self, config(autopilot={"priority": priority, "max_parallel": 1}), script)
        pipe.issue(5)
        # A red dependency scan that the owner opened, in no milestone.
        pipe.gh.add_issue(6, title="Scan fails", labels=("bug",))
        self.assertEqual(pipe.play(), [("plan", 6), ("go", 6), ("plan", 5), ("go", 5)])
        # Without `bug` in the list, the milestone scope never reaches it.
        pipe = Pipeline(self, config(autopilot={"max_parallel": 1}), script)
        pipe.issue(5)
        pipe.gh.add_issue(6, title="Scan fails", labels=("bug",))
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5)])

    def test_waves_run_in_order_whatever_the_age_of_their_issues(self):
        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return plan()
            if inputs["number"] == "7":
                pipe.issue(9)  # opened while the milestone runs, with no label
            return done()

        labels = {"journey": {"wave": 1}, "docs": {"wave": 2}}
        pipe = Pipeline(self, config(autopilot={"labels": labels}), script)
        pipe.issue(5, labels=("docs",))
        pipe.issue(6, labels=("journey",))
        pipe.issue(7)
        pipe.issue(8, labels=("journey",))
        played = pipe.play()
        index = played.index
        self.assertLess(index(("go", 9)), index(("plan", 6)))
        self.assertLess(index(("go", 7)), index(("plan", 8)))
        self.assertLess(max(index(("go", 6)), index(("go", 8))), index(("plan", 5)))
        self.assertEqual(played[-2:], [("plan", 5), ("go", 5)])

    def test_a_wave_never_waits_in_a_circle_with_blocked_by_marks(self):
        def script(pipe, inputs):
            return plan() if inputs["command"] == "plan" else done()

        labels = {"journey": {"wave": 1}, "docs": {"wave": 2}}
        pipe = Pipeline(self, config(autopilot={"labels": labels}), script)
        pipe.issue(5, labels=("journey",))
        pipe.issue(6)  # a follow-up that a person marked blocked by the docs sweep
        pipe.issue(7, labels=("docs",))
        pipe.gh.blockers[6] = [7]
        played = pipe.play()
        self.assertEqual([n for c, n in played if c == "go"], [5, 7, 6])
        self.assertTrue(all(pipe.gh.issues[n]["state"] == "closed" for n in (5, 6, 7)))

    def test_a_held_issue_starts_nothing_until_its_label_is_removed(self):
        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return plan()
            if inputs["command"] == "go" and inputs["number"] == "6":
                # A person holds it while its round runs: the round finishes, and starts
                # neither its fix round nor, later, its merge.
                pipe.gh.issues[6]["labels"] = [{"name": "on-hold"}]
                return done(checks=False)
            return done()

        settings = {"labels": {"on-hold": {"hold": True}}}
        pipe = Pipeline(self, config(autopilot=settings), script)
        pipe.issue(5, labels=("on-hold",))
        pipe.issue(6)
        self.assertEqual(pipe.play(), [("plan", 6), ("go", 6)])
        self.assertEqual(pipe.played[-1][2], "held")
        steps = {s["number"]: s for s in autopilot.preview(pipe.gh, pipe.cfg)[1]}
        self.assertEqual((steps[5]["state"], steps[6]["state"]), ("held", "held"))
        data = collect(pipe.gh, pipe.cfg, workers=1)
        self.assertEqual(data["needs_person"], [])
        [pull] = data["pull_requests"]
        self.assertEqual(pull["state"], "held")
        # Without the label, the next sweep starts what it held back, and #5 runs.
        pipe.gh.issues[5]["labels"] = pipe.gh.issues[6]["labels"] = []
        self.assertEqual(pipe.play(), [("plan", 6), ("go", 6), ("plan", 5), ("fix", 7), ("go", 5)])
        self.assertTrue(all(pipe.gh.issues[n]["state"] == "closed" for n in (5, 6)))
        self.assertEqual(read_state(pipe.gh.comments(7))[1]["auto_fixes"], 1)

    def test_a_merge_waiting_for_a_code_owner_merges_after_the_approval(self):
        def script(pipe, inputs):
            return plan() if inputs["command"] == "plan" else done()

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        pipe.gh.review_states[6] = {"decision": "REVIEW_REQUIRED"}
        pipe.gh.files["CODEOWNERS"] = "* @olivia\n"
        pipe.gh.pr_files[6] = [{"filename": "app.py", "additions": 3, "deletions": 1}]
        pipe.gh.merge_error = GitHubError(405, "Waiting on code owner review from olivia.")
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5)])
        self.assertEqual(pipe.played[-1][2], "merge_refused")
        [mention] = pipe.gh.comments_matching(6, "a person is needed")
        self.assertIn("approve the pull request as a code owner (`@olivia`)", mention["body"])
        # The owner approves; the approval's own run writes nothing, and the merge runs
        # in the issue's queue, once its step is played.
        pipe.gh.merge_error = None
        written = len(pipe.gh.comments(6)), len(pipe.gh.comments(5))
        self.assertEqual(pipe.approve(), ["#6: sent @olivia's approval to #5's queue."])
        self.assertEqual((len(pipe.gh.comments(6)), len(pipe.gh.comments(5))), written)
        self.assertEqual((pipe.gh.merged, pipe.gh.issues[5]["state"]), ([], "open"))
        [step] = [
            d for d in pipe.gh.dispatches if d["inputs"].get("note", "").startswith("approval")
        ]
        self.assertEqual(step["inputs"]["command"], "step")
        self.assertEqual(
            (step["inputs"]["number"], step["inputs"]["note"]), ("5", "approval 6 9000")
        )
        pipe.drain()
        self.assertEqual(len(pipe.gh.merged), 1)
        pipe.play()
        self.assertEqual(pipe.gh.issues[5]["state"], "closed")
        self.assertEqual(len(pipe.gh.comments_matching(6, "a person is needed")), 1)

    def test_an_approval_that_the_items_run_took_merges_once_and_reaches_the_engine_once(self):
        def script(pipe, inputs):
            return plan() if inputs["command"] == "plan" else done()

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        pipe.gh.review_states[6] = {"decision": "REVIEW_REQUIRED"}
        pipe.gh.files["CODEOWNERS"] = "* @olivia\n"
        pipe.gh.pr_files[6] = [{"filename": "app.py", "additions": 3, "deletions": 1}]
        pipe.gh.merge_error = GitHubError(405, "Waiting on code owner review from olivia.")
        pipe.play()
        self.assertEqual(pipe.played[-1][2], "merge_refused")
        # The item's group stores the state comment before the review, which comes after it.
        pipe.take_run(5)
        pipe.gh.merge_error = None
        written = item_state.read(pipe.gh, 5)[0]["created_at"]
        submitted = datetime.fromisoformat(written.replace("Z", "+00:00")) + timedelta(minutes=1)
        self.assertEqual(
            pipe.approve(at=submitted.strftime("%Y-%m-%dT%H:%M:%SZ")),
            ["#6: sent @olivia's approval to #5's queue."],
        )
        with mock.patch.object(host.engine, "answer", wraps=host.engine.answer) as answer:
            # The run takes the review before its step: the engine hears it here.
            pipe.take_run(5)
            stored = item_state.read(pipe.gh, 5)[1]
            self.assertIn("review-9000", stored["recentEvents"])
            self.assertEqual([action["kind"] for action in stored["pending"]], ["accept"])
            pipe.drain()
        self.assertEqual(len(pipe.gh.merged), 1)
        heard = [
            call.args[0]["event"]["kind"]
            for call in answer.call_args_list
            if call.args[0]["call"] == "step"
        ]
        self.assertEqual(heard.count("changeApproved"), 1)
        self.assertEqual(
            [
                call.args[0]["event"]["id"]
                for call in answer.call_args_list
                if call.args[0]["call"] == "step"
                and call.args[0]["event"]["kind"] == "changeApproved"
            ],
            ["review-9000"],
        )

    def test_an_approval_stores_its_answer_only_from_the_items_group(self):
        def script(pipe, inputs):
            return plan() if inputs["command"] == "plan" else done()

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        pipe.gh.review_states[6] = {"decision": "REVIEW_REQUIRED"}
        pipe.gh.files["CODEOWNERS"] = "* @olivia\n"
        pipe.gh.pr_files[6] = [{"filename": "app.py", "additions": 3, "deletions": 1}]
        pipe.gh.merge_error = GitHubError(405, "Waiting on code owner review from olivia.")
        pipe.play()
        self.assertEqual(pipe.played[-1][2], "merge_refused")
        # The item's group stores the state comment, as the run that took it did.
        pipe.take_run(5)
        [comment] = copy.deepcopy(pipe.state_comments(5))
        pipe.gh.merge_error = None
        # The approval's own run, in its group `approved-N`, only previews: nothing is stored.
        self.assertEqual(pipe.approve(), ["#6: sent @olivia's approval to #5's queue."])
        self.assertEqual(pipe.state_comments(5), [comment])
        # The step run in the item's group stores the engine's answer, at the next revision.
        pipe.drain()
        self.assertEqual(len(pipe.gh.merged), 1)
        # The approval, the merge's `accepted` and the result of the issue's `close`.
        stored = item_state.read(pipe.gh, 5)[1]
        self.assertEqual(stored["revision"], item_state.find([comment])[1]["revision"] + 3)
        self.assertEqual((stored["state"], stored["pending"]), ("accepted", []))
        # The approval is named after its review, as `take` names it.
        self.assertIn("review-9000", stored["recentEvents"])

    def test_an_approval_of_a_commit_that_is_no_longer_the_head_merges_nothing(self):
        def script(pipe, inputs):
            return plan() if inputs["command"] == "plan" else done()

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        pipe.gh.review_states[6] = {"decision": "REVIEW_REQUIRED"}
        pipe.gh.files["CODEOWNERS"] = "* @olivia\n"
        pipe.gh.pr_files[6] = [{"filename": "app.py", "additions": 3, "deletions": 1}]
        pipe.gh.merge_error = GitHubError(405, "Waiting on code owner review from olivia.")
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5)])
        self.assertEqual(pipe.played[-1][2], "merge_refused")
        # The owner approves the head, then a round ahead of the step moves the head and
        # ends on a commit the owner never saw.
        pipe.gh.merge_error = None
        self.assertEqual(pipe.approve(), ["#6: sent @olivia's approval to #5's queue."])
        pipe.gh.pulls[6]["head"]["sha"] = "e" * 40
        pipe.gh.round_comment(6, 9, "e" * 40, "passed", outcome="merge_refused")
        pipe.drain()
        self.assertEqual(pipe.gh.merged, [])
        self.assertEqual(pipe.gh.issues[5]["state"], "open")
        # The approval of the new head merges it.
        self.assertEqual(pipe.approve(), ["#6: sent @olivia's approval to #5's queue."])
        pipe.drain()
        self.assertEqual(pipe.gh.merged, [(6, "e" * 40, "squash")])

    def test_an_owner_who_approves_and_merges_at_once_gets_no_mention(self):
        def script(pipe, inputs):
            return plan() if inputs["command"] == "plan" else done()

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        pipe.gh.review_states[6] = {"decision": "REVIEW_REQUIRED"}
        pipe.gh.files["CODEOWNERS"] = "* @olivia\n"
        pipe.gh.pr_files[6] = [{"filename": "app.py", "additions": 3, "deletions": 1}]
        pipe.gh.merge_error = GitHubError(405, "Waiting on code owner review from olivia.")
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5)])
        self.assertEqual(len(pipe.gh.comments_matching(6, "a person is needed")), 1)
        # The owner approves and merges in the same minute, before NexKit's merge reads
        # the issue, which the merge closed through the `Closes` line.
        pipe.take_run(5)
        pipe.gh.pulls[6].update(state="closed", merged=True, merged_by={"login": "olivia"})
        pipe.gh.issues[5].update(state="closed", state_reason="completed")
        self.assertEqual(pipe.approve(), ["#6: sent @olivia's approval to #5's queue."])
        pipe.drain()
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5)])  # nothing more ran
        self.assertEqual(pipe.gh.merged, [])
        self.assertEqual(len(pipe.gh.comments_matching(6, "a person is needed")), 1)
        self.assertEqual(collect(pipe.gh, pipe.cfg, workers=1)["needs_person"], [])

    def test_a_pull_request_closed_while_its_round_ran_and_reopened_waits_for_a_person(self):
        def close(pipe, pull):
            # A person closes the pull request while its round checks and reviews it.
            pipe.gh.pulls[pull]["state"] = "closed"

        def script(pipe, inputs):
            return plan() if inputs["command"] == "plan" else {**done(), "meanwhile": close}

        pipe = Pipeline(self, config(merge={"auto": True}), script)
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5)])
        self.assertEqual((pipe.played[-1][2], pipe.gh.merged), ("pull_closed", []))
        self.assertEqual(pipe.gh.comments_matching(6, "a person is needed"), [])
        # Closed, it waits for a person, who is mentioned once since the round.
        [mention] = pipe.gh.comments_matching(5, "a person is needed")
        self.assertIn("Its pull request was closed", mention["body"])
        # Reopened, it still waits for a person, with what to do now.
        pipe.gh.pulls[6]["state"] = "open"
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5)])  # nothing more ran
        self.assertEqual(pipe.gh.merged, [])
        self.assertEqual(len(pipe.gh.comments_matching(5, "a person is needed")), 1)
        reason = autopilot.person(*autopilot.REOPENED)
        needs = collect(pipe.gh, pipe.cfg, workers=1)["needs_person"]
        self.assertEqual(needs, [{"number": 6, "kind": "pull_request", "reason": reason}])

    def test_a_held_merge_merges_once_the_label_is_removed(self):
        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return plan()
            pipe.gh.issues[5]["labels"] = [{"name": "on-hold"}]
            return done()

        settings = {"labels": {"on-hold": {"hold": True}}}
        pipe = Pipeline(self, config(autopilot=settings), script)
        pipe.issue(5)
        pipe.play()
        self.assertEqual((pipe.gh.merged, pipe.played[-1][2]), ([], "held"))
        pipe.gh.issues[5]["labels"] = []
        pipe.play()
        self.assertEqual(len(pipe.gh.merged), 1)
        self.assertEqual(pipe.gh.issues[5]["state"], "closed")

    def test_a_label_added_while_the_agent_works_holds_a_round_decided_on_the_stored_state(self):
        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return plan()
            # Added after `route` took the item's events, so the stored state is not held yet.
            pipe.gh.issues[5]["labels"] = [{"name": "on-hold"}]
            return done()

        settings = {"labels": {"on-hold": {"hold": True}}}
        pipe = Pipeline(self, config(autopilot=settings), script)
        pipe.issue(5)
        pipe.take_run(5)
        entries, fallbacks = self.round_ends()
        pipe.play()
        [(_, _, before)] = entries
        self.assertFalse(before["hold"])
        self.assertNotIn("nexkit.host.round_ended", fallbacks)
        self.assertEqual((pipe.gh.merged, pipe.played[-1][2]), ([], "held"))
        self.assertTrue(item_state.read(pipe.gh, 5)[1]["hold"])
        pipe.gh.issues[5]["labels"] = []
        pipe.play()
        self.assertEqual(len(pipe.gh.merged), 1)
        self.assertEqual(pipe.gh.issues[5]["state"], "closed")

    def test_a_label_added_again_while_the_next_round_works_holds_that_round_too(self):
        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return plan()
            # Each round's agent works while the label is added, after `route` took the events.
            pipe.gh.issues[5]["labels"] = [{"name": "on-hold"}]
            return done(checks=inputs["command"] == "fix")

        settings = {"labels": {"on-hold": {"hold": True}}}
        pipe = Pipeline(self, config(autopilot=settings), script)
        pipe.issue(5)
        pipe.take_run(5)
        entries, fallbacks = self.round_ends()
        pipe.play()
        self.assertEqual([command for command, _, _ in pipe.played], ["plan", "go"])
        self.assertEqual(pipe.played[-1][2], "held")
        self.assertTrue(item_state.read(pipe.gh, 5)[1]["hold"])
        # The label goes, the item is released and the next round starts; it is added again.
        pipe.gh.issues[5]["labels"] = []
        pipe.play()
        self.assertEqual([command for command, _, _ in pipe.played][-1], "fix")
        self.assertEqual((pipe.gh.merged, pipe.played[-1][2]), ([], "held"))
        self.assertTrue(item_state.read(pipe.gh, 5)[1]["hold"])
        self.assertNotIn("nexkit.host.round_ended", fallbacks)
        pipe.gh.issues[5]["labels"] = []
        pipe.play()
        self.assertEqual(len(pipe.gh.merged), 1)

    def test_checks_that_keep_failing_use_their_rounds_then_wait(self):
        def script(pipe, inputs):
            return plan() if inputs["command"] == "plan" else done(checks=False)

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        played = pipe.play()
        self.assertEqual(played, [("plan", 5), ("go", 5), ("fix", 6), ("fix", 6)])

    def test_a_transient_error_runs_again_after_growing_waits_then_waits_for_a_person(self):
        tries = itertools.count()
        started = []

        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return plan()
            started.append(datetime.now(UTC) + pipe.elapsed)
            return error(transient=True) if next(tries) < 2 else done()

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5), *[("go", 5)] * 3])
        self.assertEqual(pipe.gh.issues[5]["state"], "closed")
        # The second run waited 15 minutes, the third 30; no sweep started either sooner.
        waits = [round((b - a).total_seconds() / 60) for a, b in itertools.pairwise(started)]
        self.assertEqual(waits, [15, 30])
        self.assertIn("`/nexkit go` (autopilot, retry 2)", pipe.gh.run_comments(5)[-1]["body"])
        self.assertFalse(pipe.gh.comments_matching(5, "a person is needed"))

        def always(pipe, inputs):
            return plan() if inputs["command"] == "plan" else error(transient=True)

        # The fourth stop waits for a person, with every error listed.
        pipe = Pipeline(self, config(), always)
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5), *[("go", 5)] * 4])
        last = pipe.gh.run_comments(5)[-1]
        self.assertEqual(last["run"]["outcome"], "retry_failed")
        self.assertIn("Transient errors so far:\n\n1. Claude stopped.", last["body"])
        self.assertIn("\n4. Claude stopped.", last["body"])
        [mention] = pipe.gh.comments_matching(5, "a person is needed")
        self.assertIn("stopped on a transient error 4 times", mention["body"])

        # `[0]` keeps a single run again at once, and `[]` none.
        for waits, runs in (([0], 2), ([], 1)):
            pipe = Pipeline(self, config(retry={"wait_minutes": waits}), always)
            pipe.issue(5)
            self.assertEqual(pipe.play(), [("plan", 5), *[("go", 5)] * runs], waits)
            self.assertEqual(pipe.gh.run_comments(5)[-1]["run"]["outcome"], "retry_failed")
            self.assertTrue(pipe.gh.comments_matching(5, "a person is needed"))
            self.assertFalse(
                [d for d in pipe.gh.dispatches if d["inputs"]["command"] == "resume"], waits
            )

    def test_a_go_whose_review_waits_to_run_again_needs_nobody_meanwhile(self):
        # The review job leaves no result, as on 2026-11-09: the review runs again on the
        # pull request after the first wait, and the issue's `/nexkit go` waits for it.
        reviews = itertools.count()

        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return plan()
            found = done()
            if inputs["command"] == "go" and next(reviews) < 1:
                del found["review"]
            return found

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        autopilot.request(pipe.gh, "nexkit.yml", "main")
        while pipe.gh.dispatches[pipe.handled]["inputs"]["command"] != "resume":
            pipe.drain_one()
        self.assertEqual(pipe.played[-1], ("go", 5, "retry_waiting"))
        # A sweep while it waits, such as the hourly one, mentions nobody.
        autopilot.request(pipe.gh, "nexkit.yml", "main")
        pipe.gh.dispatches.append(pipe.gh.dispatches.pop(pipe.handled))
        while pipe.gh.dispatches[pipe.handled]["inputs"]["command"] != "resume":
            pipe.drain_one()
        data = collect(pipe.gh, pipe.cfg)
        self.assertEqual(data["needs_person"], [])
        [issue] = data["issues"]
        self.assertEqual((issue["state"], issue["pull"]), ("in_pull_request", 6))
        [pull] = data["pull_requests"]
        self.assertEqual(pull["state"], "retrying")
        self.assertFalse(pipe.gh.comments_matching(5, "a person is needed"))
        pipe.play()
        self.assertEqual(pipe.played[-1][::2], ("review", "merged"))
        self.assertEqual(pipe.gh.issues[5]["state"], "closed")
        self.assertFalse(pipe.gh.comments_matching(5, "a person is needed"))

    def test_a_new_command_replaces_a_run_that_waits_to_run_again(self):
        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return plan()
            return error(transient=True) if inputs["auto"] == "autopilot" else done()

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        autopilot.request(pipe.gh, "nexkit.yml", "main")
        # The waiting run is not drained: the go waits to run again.
        while pipe.handled < len(pipe.gh.dispatches):
            if pipe.gh.dispatches[pipe.handled]["inputs"]["command"] == "resume":
                break
            pipe.drain_one()
        self.assertEqual(pipe.gh.run_comments(5)[-1]["run"]["status"], "paused")
        steps = {s["number"]: s for s in collect(pipe.gh, pipe.cfg)["autopilot"]["issues"]}
        self.assertEqual(steps[5]["state"], "active")
        self.assertIn("runs again after", steps[5]["reason"])
        # A person posts the command while the waiting run waits: it replaces the wait, so
        # that run starts nothing once the time comes.
        waiting = pipe.gh.dispatches[pipe.handled]
        pipe.handled += 1
        go = {"command": "go", "number": "5", "note": "", "auto": ""}
        pipe.dispatch("nexkit.yml", "main", go)
        pipe.gh.dispatches[-1]["run_id"] = next(pipe.runs)
        pipe.drain()
        pipe.later(timedelta(minutes=15))
        pipe.waited(waiting["inputs"])
        pipe.drain()
        played = [(c, o) for c, _, o in pipe.played]
        expected = [("plan", "planned"), ("go", "retry_waiting"), ("go", "merged")]
        self.assertEqual(played[:3], expected)
        self.assertEqual(pipe.gh.issues[5]["state"], "closed")
        replaced = pipe.gh.run_comments(5)[1]["run"]
        self.assertEqual(replaced["status"], "continued")
        self.assertNotIn("resume", replaced)
        self.assertEqual([c for c, _ in played].count("go"), 2, played)

    def test_a_resume_is_started_by_a_step_in_the_items_queue_and_the_waiting_run_writes_nothing(
        self,
    ):
        pauses = itertools.count()

        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return plan()
            if inputs["command"] == "go" and next(pauses) < 1:
                at = autopilot.iso(datetime.now(UTC) + timedelta(minutes=30))
                limit = "You've hit your session limit"
                return {
                    "agent": {
                        "status": "paused",
                        "error": f"Claude hit the account's usage limit: {limit}",
                        "limit": limit,
                        "resume_at": at,
                        "reset_known": True,
                    }
                }
            return done()

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        autopilot.request(pipe.gh, "nexkit.yml", "main")
        while pipe.gh.dispatches[pipe.handled]["inputs"]["command"] != "resume":
            pipe.drain_one()
        paused = pipe.gh.run_comments(5)[-1]
        self.assertEqual(paused["run"]["status"], "paused")
        self.assertEqual(paused["run"]["resume"]["command"], "go")
        waiting = pipe.gh.dispatches[pipe.handled]
        pipe.handled += 1
        # Time passes until the resume is due, and then the waiting run starts.
        pipe.later(timedelta(minutes=31))
        comments = copy.deepcopy(pipe.gh.issue_comments)
        sent = len(pipe.gh.dispatches)
        pipe.waited(waiting["inputs"])
        # It changed no comment, and sent a step into the item's group and nothing else.
        self.assertEqual(pipe.gh.issue_comments, comments)
        new = pipe.gh.dispatches[sent:]
        self.assertEqual([d["inputs"]["command"] for d in new], ["step"])
        [step] = [d["inputs"] for d in new]
        self.assertEqual(step["note"], f"resume {paused['id']}")
        self.assertEqual(queue_item(pipe.gh, "workflow_dispatch", {"inputs": step}), 5)
        self.assertEqual([c for c, _, _ in pipe.played].count("go"), 1)
        # The step starts the command, once, and marks the paused run as continued.
        pipe.drain()
        self.assertEqual([c for c, _, _ in pipe.played].count("go"), 2)
        self.assertEqual(pipe.gh.run_comments(5)[1]["run"]["status"], "continued")
        self.assertEqual(pipe.gh.issues[5]["state"], "closed")

    def test_a_command_that_never_runs_starts_again_later_then_waits_naming_the_outage(self):
        def script(pipe, inputs):
            return "lost"

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5)] * 5)
        [mention] = pipe.gh.comments_matching(5, "a person is needed")
        self.assertIn("did not run, 5 times: GitHub created no job for it", mention["body"])

    def test_a_command_lost_in_an_outage_runs_once_github_starts_runs_again(self):
        # GitHub creates no job for the first two runs, as on 2026-11-09; then it does.
        lost = itertools.count()

        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return "lost" if next(lost) < 2 else plan()
            return done()

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        for _ in range(2):
            # The first sweep starts it; the next, such as the hourly one, finds its run
            # ended without a comment and starts it once more, at once.
            autopilot.request(pipe.gh, "nexkit.yml", "main")
            pipe.drain()
        # GitHub lost that one too: the next start waits 30 minutes after it.
        self.assertEqual(pipe.played, [("plan", 5, "lost"), ("plan", 5, "lost")])
        for minutes in (0, 20):
            pipe.later(timedelta(minutes=minutes))
            autopilot.request(pipe.gh, "nexkit.yml", "main")
            pipe.drain()
            self.assertEqual(len(pipe.played), 2, minutes)
        pipe.later(timedelta(minutes=11))
        self.assertEqual(pipe.play(), [("plan", 5), ("plan", 5), ("plan", 5), ("go", 5)])
        self.assertEqual(pipe.gh.issues[5]["state"], "closed")
        self.assertFalse(pipe.gh.comments_matching(5, "a person is needed"))

    def test_a_retry_that_never_starts_is_started_once_more_after_lost_after_minutes(self):
        # The review job leaves no result, as on 2026-11-09; the one more try it starts is
        # lost, so the round shows a review on its way that never comes.
        reviews = itertools.count()

        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return plan()
            if inputs["command"] == "go":
                found = done()
                del found["review"]
                return found
            return "lost" if next(reviews) < 1 else done()

        pipe = Pipeline(self, config(retry={"wait_minutes": [0]}), script)
        pipe.issue(5)
        autopilot.request(pipe.gh, "nexkit.yml", "main")
        pipe.drain()
        self.assertEqual(
            [(c, n, o) for c, n, o in pipe.played],
            [("plan", 5, "planned"), ("go", 5, "retry"), ("review", 6, "lost")],
        )
        # Within `lost_after_minutes`, the review is still on its way.
        pipe.later(timedelta(minutes=20))
        autopilot.request(pipe.gh, "nexkit.yml", "main")
        pipe.drain()
        self.assertEqual(len(pipe.played), 3)
        pipe.later(timedelta(minutes=11))
        autopilot.request(pipe.gh, "nexkit.yml", "main")
        pipe.drain()
        self.assertEqual(pipe.played[-1], ("review", 6, "merged"))
        self.assertEqual(pipe.gh.issues[5]["state"], "closed")
        self.assertFalse(pipe.gh.comments_matching(6, "a person is needed"))
        pipe.check(quiet=True)

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

    def test_a_lost_command_starts_once_more_at_the_end_of_another_run(self):
        # The plan of #5 never runs. The sweep at the end of the next run, the plan of #6,
        # sees that its run ended and starts it once more: no hourly schedule is needed.
        lost = itertools.count()

        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return "lost" if inputs["number"] == "5" and next(lost) < 1 else plan()
            later = {"meanwhile": lambda pipe, _: pipe.later(timedelta(minutes=31))}
            return {**done(), **(later if inputs["number"] == "6" else {})}

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        pipe.issue(6)
        autopilot.request(pipe.gh, "nexkit.yml", "main")
        pipe.drain()
        played = [(c, n, o) for c, n, o in pipe.played if o in ("lost", "planned", "merged")]
        self.assertEqual(
            played,
            [
                ("plan", 5, "lost"),
                ("plan", 6, "planned"),
                ("plan", 5, "planned"),
                ("go", 6, "merged"),
                ("go", 5, "merged"),
            ],
        )
        self.assertEqual(pipe.gh.issues[5]["state"], "closed")
        pipe.check(quiet=True)

    def test_a_refused_command_starts_once_more_at_the_end_of_its_own_run(self):
        # `route` refuses the plan that autopilot started on #5, and no other run ends
        # after it: its own run starts the sweep that starts it once more, at once.
        refused = itertools.count()

        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return "refused" if next(refused) < 1 else plan()
            return done()

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        autopilot.request(pipe.gh, "nexkit.yml", "main")
        pipe.drain()
        played = [(c, n, o) for c, n, o in pipe.played if o in ("refused", "planned", "merged")]
        self.assertEqual(
            played, [("plan", 5, "refused"), ("plan", 5, "planned"), ("go", 5, "merged")]
        )
        pipe.check(quiet=True)

    def test_a_command_that_waits_in_the_queue_is_not_started_twice(self):
        def script(pipe, inputs):
            if inputs["command"] == "plan":
                # Hours pass while its run waits in the queue, and the schedule sweeps.
                pipe.later(timedelta(hours=2))
                pipe.sweep()
                return plan()
            return done()

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5)])

    def test_a_run_that_ends_without_a_report_moves_on_at_its_own_end(self):
        stalls = itertools.count()

        def script(pipe, inputs):
            if inputs["command"] == "go":
                return "stall" if next(stalls) < 1 else done()
            return plan()

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        autopilot.request(pipe.gh, "nexkit.yml", "main")
        pipe.drain()  # no time passes, and no hourly schedule
        self.assertEqual(
            [(c, n, o) for c, n, o in pipe.played],
            [("plan", 5, "planned"), ("go", 5, "stall"), ("go", 5, "merged")],
        )
        [_, stalled, _] = [run for _, run in run_comments(pipe.gh.comments(5)) if "command" in run]
        self.assertEqual((stalled["status"], stalled["outcome"]), ("failure", "lost"))
        pipe.check(quiet=True)

    def test_a_merge_that_waits_for_required_checks_merges_when_they_pass(self):
        def script(pipe, inputs):
            return plan() if inputs["command"] == "plan" else done()

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        # The rules require `e2e`, which has not reported yet; it passes before the sweep.
        pipe.gh.required = {"e2e"}
        pipe.gh.merge_error = [GitHubError(405, 'Required status check "e2e" is expected.')]
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5)])
        self.assertEqual(pipe.gh.issues[5]["state"], "closed")

    def test_a_required_check_that_fails_while_a_merge_waits_gets_a_fix_round(self):
        def script(pipe, inputs):
            return plan() if inputs["command"] == "plan" else done()

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        pipe.gh.merge_error = [
            GitHubError(405, 'Required status check "e2e" is expected.'),
            GitHubError(405, 'Required status check "e2e" is failing.'),
        ]
        # The rules require `e2e`, which has not reported at the first try of the merge and
        # has failed at the second.
        pipe.gh.required = {"e2e"}
        merge, tries = pipe.gh.merge, []

        def merged(number, sha, method="squash"):
            tries.append(sha)
            if len(tries) == 2:
                pipe.gh.commit_checks[sha] = [{"id": 9, "name": "e2e", "conclusion": "failure"}]
            return merge(number, sha, method)

        pipe.gh.merge = merged
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5), ("fix", 6)])
        self.assertEqual([outcome for *_, outcome in pipe.played[1:]], ["merge_waiting", "merged"])
        rounds = [run for _, run in run_comments(pipe.gh.comments(6))]
        self.assertEqual(rounds[0]["outcome"], "auto_fix")
        self.assertEqual(read_state(pipe.gh.comments(6))[1]["auto_fixes"], 1)
        self.assertEqual(pipe.gh.issues[5]["state"], "closed")
        for number in (5, 6):
            self.assertFalse(pipe.gh.comments_matching(number, "a person is needed"))

    def test_a_state_comment_that_someone_else_changed_waits_for_a_person(self):
        def script(pipe, inputs):
            return plan() if inputs["command"] == "plan" else done()

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        pipe.gh.required = {"e2e"}
        pipe.gh.merge_error = GitHubError(405, 'Required status check "e2e" is expected.')
        autopilot.request(pipe.gh, "nexkit.yml", "main")
        pipe.drain()
        self.assertEqual([outcome for *_, outcome in pipe.played], ["planned", "merge_waiting"])
        # Someone else rewrites the pull request's state, so that it reads as fresh, and its
        # required check fails: merging or a fix round would follow from that state.
        [state_comment] = pipe.gh.comments_matching(6, "nexkit:state")
        recorded = read_state(pipe.gh.comments(6))[1]
        self.assertEqual(recorded["auto_fixes"], 0)
        state_comment["body"] = state_comment["body"].replace(
            STATE_PREFIX, "Edited by hand.\n\n" + STATE_PREFIX
        )
        state_comment["editors"] = [BOT_LOGIN, "mallory"]
        pipe.gh.merge_error = GitHubError(405, 'Required status check "e2e" is failing.')
        sha = pipe.gh.pulls[6]["head"]["sha"]
        pipe.gh.commit_checks[sha] = [{"id": 9, "name": "e2e", "conclusion": "failure"}]
        started = len(pipe.gh.dispatches)
        for _ in range(3):
            pipe.later(timedelta(hours=7))
            autopilot.request(pipe.gh, "nexkit.yml", "main")
            pipe.drain()
        self.assertEqual(pipe.gh.merged, [])
        self.assertEqual([command for command, *_ in pipe.played], ["plan", "go"])
        commands = [d["inputs"]["command"] for d in pipe.gh.dispatches[started:]]
        self.assertEqual([c for c in commands if c != "step"], ["autopilot"] * 3)
        self.assertEqual(pipe.gh.issues[5]["state"], "open")
        [mention] = [c for n in (5, 6) for c in pipe.gh.comments_matching(n, "a person is needed")]
        self.assertIn("`@mallory` changed a comment of NexKit's", mention["body"])
        # The status lists it with its reason.
        needs = collect(pipe.gh, pipe.cfg, workers=1)["needs_person"]
        self.assertTrue(any(autopilot.CHANGED_ENTRY in item["reason"] for item in needs), needs)
        # Deleting the comment hands the pull request back to autopilot.
        pipe.gh.issue_comments[6].remove(state_comment)
        pipe.later(timedelta(hours=7))
        autopilot.request(pipe.gh, "nexkit.yml", "main")
        pipe.drain()
        needs = collect(pipe.gh, pipe.cfg, workers=1)["needs_person"]
        self.assertFalse(any(autopilot.CHANGED_ENTRY in item["reason"] for item in needs), needs)

    def test_a_new_commit_while_a_merge_waits_is_reviewed_and_merged(self):
        def script(pipe, inputs):
            return plan() if inputs["command"] == "plan" else done()

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        pipe.gh.required = {"e2e"}
        pipe.gh.merge_error = GitHubError(405, 'Required status check "e2e" is expected.')
        autopilot.request(pipe.gh, "nexkit.yml", "main")
        pipe.drain()
        self.assertEqual([outcome for *_, outcome in pipe.played], ["planned", "merge_waiting"])
        # A person pushes to the branch, and the checks of the new commit pass.
        pipe.head(6)
        pipe.gh.merge_error = None
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5), ("review", 6)])
        self.assertEqual(pipe.played[-1][2], "merged")
        self.assertEqual(pipe.gh.merged[0][1], pipe.gh.pulls[6]["head"]["sha"])
        for number in (5, 6):
            self.assertFalse(pipe.gh.comments_matching(number, "a person is needed"))

    def test_a_merge_waiting_on_a_branch_behind_its_base_is_updated_and_merged(self):
        expected = "Repository rule violations found\n\n2 of 2 required status checks are expected."

        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return plan()
            if inputs["command"] == "go":
                return done()
            # The review that the update started: its change is the approved one.
            return {"checks": done()["checks"]}

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        # The rules require `e2e` and `lint`, which have not reported yet.
        pipe.gh.required = {"e2e", "lint"}
        pipe.gh.merge_error = GitHubError(405, expected)
        autopilot.request(pipe.gh, "nexkit.yml", "main")
        pipe.drain()
        self.assertEqual([outcome for *_, outcome in pipe.played], ["planned", "merge_waiting"])
        # `main` moves, and branch protection wants the branch up to date: the checks that
        # passed on its head stay "expected". Hours later, the next sweep updates it.
        pipe.gh.pulls[6]["mergeable_state"] = "behind"
        pipe.gh.merge_error = [GitHubError(405, expected)]
        pipe.later(timedelta(hours=7))
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5), ("review", 6)])
        self.assertEqual(pipe.played[-1][2], "merged")
        self.assertEqual(len(pipe.gh.updated), 1)
        self.assertEqual(pipe.gh.merged[0][1], "e" * 40)
        self.assertEqual(pipe.reviews, 1)
        rounds = [run["outcome"] for _, run in run_comments(pipe.gh.comments(6))]
        self.assertIn("branch_updated", rounds)
        self.assertEqual(pipe.gh.issues[5]["state"], "closed")
        for number in (5, 6):
            self.assertFalse(pipe.gh.comments_matching(number, "a person is needed"))

    def test_a_base_branch_move_waits_for_the_implement_round(self):
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

    def test_a_base_branch_move_during_a_round_starts_the_conflict_round_at_its_end(self):
        pushed = []

        def move_base(pipe, pull):
            # A merge into `main` while the automatic fix round runs: the pull request now
            # conflicts, and the check that the merge starts holds the conflict round back.
            pipe.gh.branches["main"] = "e" * 40
            pipe.gh.pulls[pull]["mergeable"] = False
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
                return done(checks=False)
            if inputs["auto"] == "conflicts":
                # The conflict round merges `main`.
                pipe.gh.pulls[int(inputs["number"])]["mergeable"] = True
                return done()
            # The automatic fix round stops on an error, without trying a merge.
            return {**error(), "meanwhile": move_base}

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        autopilot.request(pipe.gh, "nexkit.yml", "main")
        pipe.drain()  # no time passes, and no hourly schedule
        self.assertEqual(pushed, [[]])
        self.assertEqual(
            [(c, n, o) for c, n, o in pipe.played],
            [
                ("plan", 5, "planned"),
                ("go", 5, "auto_fix"),
                ("fix", 6, "agent_error"),
                ("fix", 6, "merged"),
            ],
        )
        conflict_rounds = [d for d in pipe.gh.dispatches if d["inputs"].get("auto") == "conflicts"]
        self.assertEqual(len(conflict_rounds), 1)
        self.assertEqual(pipe.gh.issues[5]["state"], "closed")
        pipe.check(quiet=True)

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

    def behind(self, pipe, pull):
        """Branch protection wants the branch up to date with `main`, which moved while the
        round reviewed: GitHub reports it as behind and refuses the merge, and NexKit merges
        `main` into it."""
        pipe.gh.pulls[pull]["mergeable_state"] = "behind"
        pipe.gh.merge_error = [
            GitHubError(405, "Head branch is out of date. Review and try again.")
        ]

    def test_a_clean_update_from_the_base_keeps_the_approval_and_merges_after_its_checks(self):
        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return plan()
            if inputs["command"] == "go":
                return {**done(), "meanwhile": self.behind}
            # The review that the update started: its change is the approved one.
            return {"checks": done()["checks"]}

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5), ("review", 6)])
        self.assertEqual([o for *_, o in pipe.played[1:]], ["branch_updated", "merged"])
        # One review agent ran, for the implement round; the new head merged without one.
        self.assertEqual(pipe.reviews, 1)
        self.assertEqual(pipe.gh.merged[0][1], "e" * 40)
        self.assertEqual(len(pipe.gh.created_reviews), 1)
        last = list(run_comments(pipe.gh.comments(6)))[-1]
        self.assertEqual(last[1]["carried"], pipe.gh.updated[0][1])
        self.assertIn("carried over, so no review agent ran", last[0]["body"])
        review_status = [s for s in pipe.gh.statuses if s["sha"] == "e" * 40]
        self.assertIn("carried over", review_status[-1]["description"])
        self.assertEqual(pipe.gh.issues[5]["state"], "closed")
        for number in (5, 6):
            self.assertFalse(pipe.gh.comments_matching(number, "a person is needed"))

    def test_a_check_whose_paths_the_update_did_not_change_gets_no_runner(self):
        unit, integration = "make unit", "scripts/integration.sh"

        def result(name, run):
            return {"name": name, "run": run, "exit_code": 0, "passed": True, "output": ""}

        def play(paths):
            def behind(pipe, pull):
                # `main` moved with commits that touch `paths` only.
                self.behind(pipe, pull)
                head = pipe.gh.pulls[pull]["head"]["sha"]
                files = [{"filename": path} for path in paths]
                pipe.gh.compares[(head, "e" * 40)] = {"status": "ahead", "files": files}

            both = [result("unit", unit), result("integration", integration)]

            def script(pipe, inputs):
                if inputs["command"] == "plan":
                    return plan()
                if inputs["command"] == "go":
                    return {**done(), "checks": both, "meanwhile": behind}
                return {"checks": both}

            cfg = config(
                parallel_checks=True,
                checks=[
                    {"name": "unit", "run": unit},
                    {"name": "integration", "run": integration, "unaffected_by": ["docs/**"]},
                ],
            )
            pipe = Pipeline(self, cfg, script)
            decisions, stages = [], pipe.stages

            def recorded(decision, outcome, artifacts):
                # Only the check jobs of the matrix run and leave results.
                decisions.append(decision)
                matrix = check_matrix(cfg, decision.get("skipped_checks"))["include"]
                jobs = {entry["group"] for entry in matrix}
                if "checks" in outcome:
                    outcome["checks"] = [r for r in outcome["checks"] if r["name"] in jobs]
                return stages(decision, outcome, artifacts)

            pipe.stages = recorded
            pipe.issue(5)
            self.assertEqual(pipe.play(), [("plan", 5), ("go", 5), ("review", 6)])
            self.assertEqual([o for *_, o in pipe.played[1:]], ["branch_updated", "merged"])
            rounds = [run for _, run in run_comments(pipe.gh.comments(6)) if "round" in run]
            return pipe, decisions[-1], rounds

        pipe, review, (go, update) = play(["docs/setup.md", "docs/img/flow.png"])
        self.assertEqual(review["skipped_checks"], [{"name": "integration", "since": go["head"]}])
        matrix = check_matrix(pipe.cfg, review["skipped_checks"])["include"]
        self.assertEqual([entry["group"] for entry in matrix], ["unit"])
        # The round passes on the other check, and records the skipped one on its head.
        self.assertEqual((update["head"], update["checks"]), ("e" * 40, "passed"))
        self.assertEqual(
            update["passed"],
            {"unit": check_fingerprint(unit), "integration": check_fingerprint(integration)},
        )
        self.assertEqual(update["setup"], go["setup"])
        last = list(run_comments(pipe.gh.comments(6)))[-1][0]["body"]
        line = f"`integration` skipped: no path it reads changed since `{go['head'][:7]}`."
        self.assertIn(line, last)
        self.assertEqual(pipe.gh.issues[5]["state"], "closed")

        # A path the check reads: it runs in its own job.
        pipe, review, (_, update) = play(["docs/setup.md", "src/api.py"])
        self.assertNotIn("skipped_checks", review)
        self.assertEqual(len(check_matrix(pipe.cfg)["include"]), 2)
        self.assertEqual(update["checks"], "passed")

    def test_an_update_that_changes_the_change_or_conflicts_is_reviewed_again(self):
        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return plan()
            if inputs["command"] == "go":
                return {**done(), "meanwhile": self.behind}
            # The merge commit changed the pull request's own change.
            return {**done(), "changed": True}

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5), ("review", 6)])
        self.assertEqual([o for *_, o in pipe.played[1:]], ["branch_updated", "merged"])
        self.assertEqual(pipe.reviews, 2)
        last = list(run_comments(pipe.gh.comments(6)))[-1][0]["body"]
        self.assertIn("The AI review ran again rather than carry over the approval", last)

        def conflicting(pipe, pull):
            # `main` moved and now conflicts: a conflict round merges it, then reviews.
            pipe.gh.pulls[pull]["mergeable"] = False
            pipe.gh.merge_error = [GitHubError(405, "Head branch is out of date.")]

        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return plan()
            if inputs["command"] == "go":
                return {**done(), "meanwhile": conflicting}
            pipe.gh.pulls[int(inputs["number"])]["mergeable"] = True
            return done()

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5), ("fix", 6)])
        self.assertEqual([o for *_, o in pipe.played[1:]], ["conflict_fix", "merged"])
        self.assertEqual(pipe.reviews, 2)
        self.assertFalse(pipe.gh.updated)

    def test_failing_checks_after_a_clean_update_start_a_counted_fix_round(self):
        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return plan()
            if inputs["command"] == "go":
                return {**done(), "meanwhile": self.behind}
            if inputs["command"] == "review":
                # The approval carries over, but `main`'s change breaks the checks.
                return {"checks": done(checks=False)["checks"]}
            return done()

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5), ("review", 6), ("fix", 6)])
        self.assertEqual([o for *_, o in pipe.played[1:]], ["branch_updated", "auto_fix", "merged"])
        # The implement round's review and the fix round's; none for the update.
        self.assertEqual(pipe.reviews, 2)
        self.assertEqual(read_state(pipe.gh.comments(6))[1]["auto_fixes"], 1)
        for number in (5, 6):
            self.assertFalse(pipe.gh.comments_matching(number, "a person is needed"))

    def test_a_base_branch_that_moves_during_the_implement_round_is_merged_before_checks(self):
        def moved(pipe):
            # Another pull request merges into `main` while the agent works.
            pipe.gh.branches["main"] = "e" * 40

        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return plan()
            return {**done(), "working": moved}

        def play(catch_up):
            pipe = Pipeline(self, config(), script)
            pipe.gh.strict = True
            pipe.catch_up = catch_up
            pipe.issue(5)
            return pipe, pipe.play()

        pipe, played = play(True)
        # `publish` merged the new tip after the round's commit, so the round's checks count
        # and it merges at once: no update of the branch and no second round.
        self.assertEqual(played, [("plan", 5), ("go", 5)])
        self.assertEqual(pipe.played[-1][2], "merged")
        self.assertEqual(pipe.gh.updated, [])
        rounds = [run for _, run in run_comments(pipe.gh.comments(6)) if "round" in run]
        self.assertEqual([run["outcome"] for run in rounds], ["merged"])
        self.assertTrue(pipe.gh.comments_matching(6, "Merged `main` (`eeeeeee`) after this round"))
        self.assertEqual(pipe.gh.issues[5]["state"], "closed")

        # Before: the round checked a head behind `main`, whose merge GitHub refused, and a
        # second round ran every check again.
        pipe, played = play(False)
        self.assertEqual(played, [("plan", 5), ("go", 5), ("review", 6)])
        self.assertEqual([o for *_, o in pipe.played[1:]], ["branch_updated", "merged"])

    def test_a_head_that_conflicts_with_a_moved_base_goes_straight_to_its_conflict_round(self):
        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return plan()
            if inputs["command"] == "go":
                # `main` moved while the agent worked, and its tip conflicts with the
                # round's commit, which `publish` finds with git.
                moved = {"working": lambda pipe: pipe.gh.branches.update(main="e" * 40)}
                return {**done(), **moved, "publish": {"conflicts": ["app.py"]}}
            return done()

        pipe = Pipeline(self, config(), script)
        pipe.gh.strict = True
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5), ("fix", 6)])
        self.assertEqual([o for *_, o in pipe.played[1:]], ["conflict_fix", "merged"])
        # The implement round ran neither checks nor review; the conflict round ran both.
        self.assertEqual(pipe.reviews, 1)
        first, second = [run for _, run in run_comments(pipe.gh.comments(6)) if "round" in run]
        self.assertEqual((first["checks"], first["verdict"]), ("not run", "not run"))
        self.assertTrue(second["conflicts"])
        self.assertEqual(read_state(pipe.gh.comments(6))[1]["auto_fixes"], 0)
        for number in (5, 6):
            self.assertFalse(pipe.gh.comments_matching(number, "a person is needed"))

    def test_rounds_in_their_checks_stop_when_the_base_branch_moves_and_one_round_follows(self):
        moves = itertools.count(1)
        seen = set()

        def merged_elsewhere(conflicting):
            def checking(pipe, pull):
                # Another pull request merges into `main` while this round runs its checks,
                # once per pull request; the push starts NexKit's check.
                if pull in seen:
                    return
                seen.add(pull)
                tip = f"{0xE0 + next(moves):040x}"
                pipe.gh.branches["main"] = tip
                head = pipe.gh.pulls[pull]["head"]["sha"]
                pipe.gh.compares[(tip, head)] = {"behind_by": 1}
                pipe.gh.pulls[pull]["mergeable_state"] = "behind"
                if conflicting:
                    pipe.gh.pulls[pull]["mergeable"] = False
                event = {"ref": "refs/heads/main", "repository": {"default_branch": "main"}}
                decision = route(pipe.gh, "push", event)
                maintain(pipe.gh, decision, pipe.cfg, workflow="nexkit.yml", ref="main")

            return checking

        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return plan()
            number = int(inputs["number"])
            if inputs["command"] == "go" and number == 5:
                # Its checks fail, and its automatic fix round is the one stopped.
                return done(checks=False)
            if inputs["command"] == "go":
                # #7's head conflicts once `main` moves.
                return {**done(), "checking": merged_elsewhere(conflicting=number == 7)}
            if inputs["auto"] == "conflicts":
                pipe.gh.pulls[number]["mergeable"] = True
                pipe.gh.pulls[number].pop("mergeable_state", None)
            return {**done(), "checking": merged_elsewhere(conflicting=False)}

        pipe = Pipeline(self, config(), script)
        pipe.gh.strict = True
        for number in (5, 6, 7):
            pipe.issue(number)
        pipe.play()
        stopped = [(c, n) for c, n, o in pipe.played if o == "superseded"]
        self.assertEqual(sorted(stopped), [("fix", 8), ("go", 6), ("go", 7)], pipe.played)
        # No round was started twice, which `route` would have refused.
        self.assertFalse([run for run in pipe.played if "nothing to add" in run[2]], pipe.played)
        for pull in [p["number"] for p in pipe.gh.pulls.values()]:
            rounds = [run for _, run in run_comments(pipe.gh.comments(pull)) if "round" in run]
            stopped = [i for i, run in enumerate(rounds) if run["outcome"] == "superseded"]
            self.assertEqual(len(stopped), 1, f"#{pull}: {rounds}")
            # Exactly one round follows each: an update with a review, or a conflict round.
            follow = rounds[stopped[0] + 1 :]
            self.assertTrue(follow, f"#{pull}")
            self.assertIn(follow[0]["trigger"].split(" ")[0], ("review", "fix"))
            # It shows as stopped by NexKit, not as a run a person cancelled.
            self.assertEqual(rounds[stopped[0]]["status"], "success")
        self.assertEqual({number for number, *_ in pipe.gh.merged}, set(pipe.gh.pulls))
        self.assertEqual(sorted(pipe.gh.cancelled), sorted(set(pipe.gh.cancelled)))
        # The automatic fix round that NexKit stopped gave its round back.
        auto_fixes = [read_state(pipe.gh.comments(pull))[1]["auto_fixes"] for pull in pipe.gh.pulls]
        self.assertEqual(auto_fixes, [0, 0, 0])
        for number in (5, 6, 7):
            self.assertEqual(pipe.gh.issues[number]["state"], "closed")
            self.assertFalse(pipe.gh.comments_matching(number, "a person is needed"))

    def test_checks_that_passed_in_a_stopped_round_get_their_commit_in_the_next_round(self):
        unit, e2e = "scripts/unit.sh", "scripts/e2e.sh"

        def result(name, run, passed, output=""):
            code = 0 if passed else 1
            return {"name": name, "run": run, "exit_code": code, "passed": passed, "output": output}

        def checking(pipe, pull):
            # The check job of `unit` finished: `unit` passed and `types` failed on the
            # head; `e2e` still runs when another merge moves `main`.
            if pipe.gh.cancelled:
                return
            run_id = int(os.environ["GITHUB_RUN_ID"])
            head = pipe.gh.pulls[pull]["head"]["sha"]
            data = io.BytesIO()
            with zipfile.ZipFile(data, "w") as archive:
                results = [result("unit", unit, True), result("types", "tsc", False)]
                archive.writestr("checks.json", json.dumps(results))
                archive.writestr("checked.json", json.dumps({"head": head}))
            pipe.gh.zips[run_id] = data.getvalue()
            artifact = {"id": run_id, "name": "nexkit-checks-unit", "size_in_bytes": 100}
            pipe.gh.run_artifact_list[run_id] = [artifact]
            tip = "e" * 40
            pipe.gh.branches["main"] = tip
            pipe.gh.compares[(tip, head)] = {"behind_by": 1}
            pipe.gh.pulls[pull]["mergeable_state"] = "behind"
            event = {"ref": "refs/heads/main", "repository": {"default_branch": "main"}}
            decision = route(pipe.gh, "push", event)
            maintain(pipe.gh, decision, pipe.cfg, workflow="nexkit.yml", ref="main")

        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return plan()
            if inputs["command"] == "go":
                return {**done(), "checking": checking}
            return done()

        cfg = config(
            checks=[{"name": "unit", "run": unit}, {"name": "e2e", "run": e2e}],
            parallel_checks=True,
        )
        pipe = Pipeline(self, cfg, script)
        pipe.gh.strict = True
        decisions = []
        stages = pipe.stages

        def recorded(decision, outcome, artifacts):
            # The check job of the next round: `unit` gets the commit it passed on and
            # skips its work, which still exits 0; `e2e` runs in full.
            decisions.append(decision)
            if "checks" in outcome and decision.get("check_heads"):
                outcome["checks"] = [
                    result("unit", unit, True, "Unchanged since the commit it passed on."),
                    result("e2e", e2e, True),
                ]
            return stages(decision, outcome, artifacts)

        pipe.stages = recorded
        pipe.issue(5)
        pipe.play()
        self.assertEqual(
            [(c, o) for c, _, o in pipe.played],
            [("plan", "planned"), ("go", "superseded"), ("review", "merged")],
        )
        [pull] = pipe.gh.pulls
        rounds = [run for _, run in run_comments(pipe.gh.comments(pull)) if "round" in run]
        stopped, review = rounds
        self.assertEqual(stopped["outcome"], "superseded")
        # Only the check that passed: not the one that failed, nor the one still running.
        self.assertEqual(stopped["passed"], {"unit": check_fingerprint(unit)})
        self.assertEqual(
            decisions[-1]["check_heads"], {"unit": {check_fingerprint(unit): stopped["head"]}}
        )
        # The round passes as it would have without the record.
        self.assertEqual((review["checks"], review["outcome"]), ("passed", "merged"))
        self.assertEqual(pipe.gh.issues[5]["state"], "closed")

    def test_a_sharded_check_runs_as_many_shards_as_each_round_counts(self):
        from nexkit import checks

        e2e = "scripts/e2e.sh"
        sharded = {"name": "e2e", "run": e2e, "shards": {"count": "node count.mjs", "max": 6}}
        cfg = config(checks=[sharded], parallel_checks=True)
        # What `count` prints in each round: the go, then two automatic fix rounds.
        counts = iter([3, 3, 4])
        verdicts = iter(["request_changes", "request_changes", "approve"])

        def script(pipe, inputs):
            return plan() if inputs["command"] == "plan" else done(verdict=next(verdicts))

        pipe = Pipeline(self, cfg, script)
        stages, seen = pipe.stages, []

        def counted(decision, outcome, artifacts):
            # The `shards` job counts; each shard is a check of its own, with its commit
            # from the rounds that split the check the same way.
            if "checks" not in outcome:
                return stages(decision, outcome, artifacts)
            n = next(counts)
            expanded = checks.expand(pipe.cfg, {"e2e": n})
            heads = decision.get("check_heads") or {}
            seen.append(
                {
                    c["name"]: (heads.get(c["name"]) or {}).get(
                        check_fingerprint(c["run"], c["shard"])
                    )
                    for c in expanded["checks"]
                }
            )
            outcome["checks"] = [
                {
                    "name": c["name"],
                    "run": e2e,
                    "exit_code": 0,
                    "passed": True,
                    "output": "",
                    "shard": c["shard"],
                }
                for c in expanded["checks"]
            ]
            return stages(decision, outcome, artifacts)

        pipe.stages = counted
        pipe.issue(5)
        pipe.play()
        self.assertEqual([len(found) for found in seen], [3, 3, 4])
        first, second, third = seen
        self.assertEqual(set(first.values()), {None})
        # Three shards again: each gets the commit on which it passed.
        self.assertTrue(all(second.values()), second)
        # Four shards: none of them is the shard that passed before.
        self.assertEqual(set(third.values()), {None})
        [pull] = pipe.gh.pulls
        rounds = [run for _, run in run_comments(pipe.gh.comments(pull)) if "round" in run]
        self.assertEqual(sorted(rounds[-1]["passed"]), ["e2e-1", "e2e-2", "e2e-3", "e2e-4"])
        self.assertEqual(pipe.gh.issues[5]["state"], "closed")

    def test_a_pull_request_whose_change_is_already_on_its_base_branch_closes(self):
        def play(on_base, refusals):
            def landed(pipe, pull):
                # Another pull request lands the same change on `main` while this round
                # reviews; the merge of `main` that NexKit makes then leaves no change.
                pipe.gh.on_base.update(on_base)
                pipe.gh.pulls[pull]["mergeable_state"] = "behind"

            def script(pipe, inputs):
                if inputs["command"] == "plan":
                    return plan()
                if inputs["command"] == "go":
                    return {**done(), "meanwhile": landed}
                return done()

            pipe = Pipeline(self, config(), script)
            pipe.issue(5)
            # `main` moves before each merge, and branch protection wants it up to date.
            pipe.gh.merge_error = [GitHubError(405, "Head branch is out of date")] * refusals
            self.assertEqual(pipe.play(), [("plan", 5), ("go", 5), ("review", 6)])
            return pipe

        pipe = play({"e" * 40}, 5)
        # NexKit merged `main` into it, after which it changes nothing: no review runs, and
        # it closes without merging, with its issue.
        outcomes = [outcome for *_, outcome in pipe.played[1:]]
        self.assertEqual(outcomes, ["branch_updated", "on_base"])
        self.assertEqual(len(pipe.gh.updated), 1)
        self.assertEqual(pipe.gh.merged, [])
        self.assertEqual(pipe.gh.pulls[6]["state"], "closed")
        self.assertEqual(pipe.gh.issues[5]["state_reason"], "completed")
        # The round that brought it up to date shows how it ended.
        [latest] = [run for _, run in run_comments(pipe.gh.comments(6)) if "round" in run]
        self.assertEqual((latest["status"], latest["outcome"]), ("success", "already_on_base"))
        self.assertTrue(pipe.gh.comments_matching(5, "`main` already holds its change"))
        data = collect(pipe.gh, pipe.cfg, workers=1)
        self.assertEqual(
            [(item["number"], item["outcome"]) for item in data["recent_closed"]],
            [(6, "already_on_base")],
        )
        self.assertEqual(data["needs_person"], [])
        for number in (5, 6):
            self.assertFalse(pipe.gh.comments_matching(number, "a person is needed"))

        # A pull request whose own change differs from `main`, however small, is reviewed
        # and merged as usual.
        pipe = play(set(), 1)
        outcomes = [outcome for *_, outcome in pipe.played[1:]]
        self.assertEqual(outcomes, ["branch_updated", "merged"])
        self.assertEqual(pipe.gh.issues[5]["state"], "closed")
        self.assertEqual(collect(pipe.gh, pipe.cfg, workers=1)["recent_closed"], [])

    def test_a_fix_round_whose_change_is_already_on_its_base_branch_closes(self):
        # The checks fail, and the base branch gets the same change before the automatic fix
        # round starts: its `agent` job finds that before setup and the agent.
        found = {
            "status": "already_on_base",
            "error": "`main` already holds the change of this pull request.",
            "merge_base": "c" * 40,
            "on_base": "b" * 40,
        }

        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return plan()
            return done(checks=False) if inputs["command"] == "go" else {"agent": found}

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5), ("fix", 6)])
        self.assertEqual(pipe.played[-1][2], "already_on_base")
        self.assertEqual(pipe.gh.pulls[6]["state"], "closed")
        self.assertEqual(pipe.gh.issues[5]["state_reason"], "completed")
        self.assertTrue(pipe.gh.comments_matching(5, f"brought by {'b' * 40}"))
        # The round ran no agent, so it gives its automatic round back.
        self.assertEqual(read_state(pipe.gh.comments(6))[1]["auto_fixes"], 0)

    def test_a_fix_round_with_a_cause_outside_the_code_checks_again_once(self):
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
        self.assertIn(cause.rstrip(".").replace("`", "'"), mention["body"])

    def round_ends(self):
        """Watch the ends of the rounds that `report` decides: the stored state of the item on
        entry to `host.round_ended`, as (action, head, state) for each round, and the
        fallbacks to the markers' document that `round_ended` made (`host.on_fallback`).
        Returns (entries, fallbacks)."""
        entries, fallbacks = [], []
        real = host.round_ended

        def spy(gh, cfg, decision, pr, head, *args, **kwargs):
            state = copy.deepcopy(item_state.read(gh, decision["issue"])[1])
            entries.append((decision["action"], head, state))
            return real(gh, cfg, decision, pr, head, *args, **kwargs)

        patches = (
            mock.patch.object(host, "round_ended", spy),
            mock.patch.object(host, "on_fallback", lambda origin, why: fallbacks.append(origin)),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return entries, fallbacks

    def test_a_go_round_is_stored_before_its_checks_and_they_decide_on_that_state(self):
        working = []

        def during(pipe):
            # The agent works: the stored state has its stage pending, as `/nexkit go` left it.
            doc = item_state.read(pipe.gh, 5)[1]
            working.append((doc["state"], [a["stage"] for a in doc["pending"] if "stage" in a]))

        def script(pipe, inputs):
            return plan() if inputs["command"] == "plan" else {**done(), "working": during}

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        pipe.take_run(5)
        entries, fallbacks = self.round_ends()
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5)])
        self.assertEqual(working, [("working", ["implement"])])
        [(action, head, state)] = entries
        # The agent's result and the publish result reached the state before the checks did.
        self.assertEqual(action, "implement")
        self.assertEqual(state["state"], "checking")
        self.assertEqual(state["change"], {"id": "6", "version": head})
        checks = [a for a in state["pending"] if a["kind"] == "runChecks"]
        self.assertEqual([a["changeVersion"] for a in checks], [head])
        self.assertFalse([a for a in state["pending"] if a["kind"] == "runAgent"])
        self.assertTrue(any(name.startswith("publish-6-") for name in state["recentEvents"]))
        self.assertNotIn("nexkit.host.round_ended", fallbacks)
        self.assertEqual(pipe.gh.issues[5]["state"], "closed")

    def test_a_fix_round_that_changed_nothing_is_stored_with_its_head_before_its_checks(self):
        outside = {"cause": "The registry failed.", "cause_outside": True}

        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return plan()
            if inputs["command"] == "go":
                return done(checks=False)
            found = {**done(checks=True), "recheck": True}
            found["agent"] = {
                **found["agent"],
                "recheck": True,
                "output": {
                    "status": "done",
                    "summary": "Nothing to change.",
                    "checks_run": [],
                    **outside,
                },
            }
            return found

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        pipe.take_run(5)
        entries, fallbacks = self.round_ends()
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5), ("fix", 6)])
        [(first, head, _), (second, again, state)] = entries
        self.assertEqual((first, second), ("implement", "fix"))
        # The fix round published the head that the round before it checked.
        self.assertEqual(again, head)
        self.assertEqual(state["state"], "checking")
        self.assertEqual(state["rounds"]["recheck"], head)
        self.assertEqual(state["rounds"]["fixed"], head)
        self.assertNotIn("nexkit.host.round_ended", fallbacks)
        self.assertEqual(pipe.gh.issues[5]["state"], "closed")

    def test_an_answer_that_only_closes_the_issue_closes_it(self):
        finding = "8b9cb87 already fixed it: the last 20 runs of `e2e` on `main` passed."

        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return plan()
            question = {
                "question": "The failure is already fixed. Close the issue?",
                "options": ["Close it as fixed by 8b9cb87", "Keep it open"],
                "recommended": 1,
                "closes": 1,
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

    def test_a_persons_answer_that_only_closes_the_issue_closes_it_without_a_round(self):
        # As in phuongnse/axis#3561: the agent cannot reproduce a flaky failure and asks.
        close = "Close the issue as not reproduced, with no code change"

        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return plan()
            question = {
                "question": "The failure does not reproduce. How should this go on?",
                "options": ["Keep the issue open until someone attaches the logs", close],
                "recommended": 0,
                "closes": 2,
            }
            output = {"status": "blocked", "blocker": "No repro.", "questions": [question]}
            return {"agent": {"status": "blocked", "error": "No repro.", "output": output}}

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5)])
        [mention] = pipe.gh.comments_matching(5, "a person is needed")
        [round_] = pipe.gh.comments_matching(5, "Decisions needed")
        self.assertIn(f"- {close} (NexKit closes the issue as not planned)", round_["body"])
        # The owner picks it, with the command; no agent runs.
        event = {
            "action": "created",
            "issue": {"number": 5},
            "comment": {"id": 99, "body": f"/nexkit go {close}", "user": {"login": "olivia"}},
        }
        decision = route(pipe.gh, "issue_comment", event, cfg=pipe.cfg)
        maintain(pipe.gh, decision, pipe.cfg, workflow="nexkit.yml", ref="main")
        self.assertEqual(
            (pipe.gh.issues[5]["state"], pipe.gh.issues[5]["state_reason"]),
            ("closed", "not_planned"),
        )
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5)])  # nothing more ran
        self.assertEqual(len(pipe.gh.comments_matching(5, "a person is needed")), 1)

    def test_a_blocked_round_whose_recommended_answer_only_closes_the_issue_mentions_the_person(
        self,
    ):
        # The report mentions the person, and the sweep that follows does not mention them again.
        close = "Close the issue as not reproduced, with no code change"
        closing = {
            "question": "The failure does not reproduce. How should this go on?",
            "options": ["Keep the issue open until someone attaches the logs", close],
            "recommended": 2,
            "closes": 2,
        }
        replanning = {
            "question": "The change is wider than planned. How should this go on?",
            "options": ["Narrow it", "Split it"],
            "recommended": 2,
            "closes": 0,
            "replans": [2],
        }
        for questions in ([closing], [closing, replanning]):

            def script(pipe, inputs, questions=questions):
                if inputs["command"] == "plan":
                    return plan()
                output = {"status": "blocked", "blocker": "No repro.", "questions": questions}
                return {"agent": {"status": "blocked", "error": "No repro.", "output": output}}

            with self.subTest(questions=len(questions)):
                pipe = Pipeline(self, config(), script)
                pipe.issue(5)
                self.assertEqual(pipe.play(), [("plan", 5), ("go", 5)])
                [mention] = pipe.gh.comments_matching(5, "a person is needed")
                self.assertFalse(pipe.gh.comments_matching(5, "carries on from here"))
                self.assertGreater(mention["id"], pipe.gh.run_comments(5)[-1]["id"])
                self.assertEqual(pipe.gh.issues[5]["state"], "open")
                self.assertEqual(
                    collect(pipe.gh, pipe.cfg, workers=1)["needs_person"],
                    [{"number": 5, "kind": "issue", "reason": autopilot.CLOSE_UNFOUNDED}],
                )

    def test_a_plan_whose_recommended_answer_only_closes_the_issue_mentions_once(self):
        close = "Close the issue as not wanted, with no change"

        def script(pipe, inputs):
            question = {
                "question": "Is this still wanted?",
                "options": ["Add it", close],
                "recommended": 2,
                "closes": 2,
            }
            return plan(questions=[question])

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5)])
        self.assertEqual(pipe.gh.issues[5]["state"], "open")
        self.assertFalse(pipe.gh.comments_matching(5, "NexKit autopilot carries on"))
        [run] = [r for _, r in run_comments(pipe.gh.comments(5)) if r.get("command") == "plan"]
        self.assertIn("answer the plan's questions", run["attention"])
        [mention] = pipe.gh.comments_matching(5, "@olivia a person is needed")
        self.assertIn("(NexKit closes the issue as not planned)", mention["body"])

    def test_a_blocked_round_that_recommends_waiting_does_not_close_the_issue(self):
        # The agent says in `no_change` that it found none, and recommends waiting.
        text = "Not a no-change finding: the issue is blocked on #1848, which is not merged."
        wait = "Wait until #1848 is merged, then run this plan again"

        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return plan()
            question = {
                "question": "The part this needs, #1848, is not on `main`.",
                "options": [wait, "Close the issue"],
                "recommended": 1,
                "closes": 0,
            }
            output = {"status": "blocked", "blocker": "#1848 is not merged.", "no_change": text}
            output["questions"] = [question]
            return {"agent": {"status": "blocked", "error": "Blocked.", "output": output}}

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        # Answered as any blocked round, once in a row; then a person decides.
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5), ("go", 5)])
        self.assertEqual(pipe.gh.issues[5]["state"], "open")
        self.assertFalse(pipe.gh.comments_matching(5, "NexKit autopilot closes this issue"))
        [answered] = pipe.gh.comments_matching(5, "answers for the owner")
        self.assertIn(wait, answered["body"])
        self.assertTrue(pipe.gh.comments_matching(5, "a person is needed"))

    def test_a_plan_whose_change_is_in_protected_paths_waits_for_a_person(self):
        def script(pipe, inputs):
            return plan(protected_paths=[".github/workflows/ci.yml"])

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5)])
        [plan_comment] = pipe.gh.comments_matching(5, "### Needs a hand-made change")
        self.assertIn("#the-plans-change-is-in-protected-paths", plan_comment["body"])
        [mention] = pipe.gh.comments_matching(5, "@olivia a person is needed")
        self.assertIn(
            "make the change in the protected paths (`.github/workflows/ci.yml`)", mention["body"]
        )

    def test_a_blocked_round_whose_change_is_in_protected_paths_gets_no_answer(self):
        # The round recommends that a person make the workflow change by hand.
        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return plan()
            found = blocked()
            found["agent"]["output"]["protected_paths"] = [".github/workflows/ci.yml"]
            return found

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5)])
        self.assertFalse(pipe.gh.comments_matching(5, "answers for the owner"))
        [mention] = pipe.gh.comments_matching(5, "@olivia a person is needed")
        self.assertIn("make the change in the protected paths", mention["body"])

    def test_a_plan_that_finds_an_outside_failure_passed_since_closes_the_issue(self):
        finding = "The .NET download host answered 409 in every job of run 50."
        passed = {
            "id": 61,
            "url": "https://github.com/acme/app/actions/runs/61",
            "sha": "c" * 40,
            "workflow": "ci.yml",
            "branch": "main",
        }

        def script(pipe, inputs):
            return plan(no_change=finding, passed_since=[passed])

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5)])
        self.assertEqual(pipe.gh.issues[5]["state"], "closed")
        [closing] = pipe.gh.comments_matching(5, "NexKit autopilot closes this issue")
        self.assertIn("with the finding of [its plan]", closing["body"])
        self.assertIn(finding, closing["body"])
        run = "[run 61 of `ci.yml`](https://github.com/acme/app/actions/runs/61)"
        line = f"Passed on `main` since: {run} at ccccccc."
        self.assertIn(line, closing["body"])
        self.assertFalse(pipe.gh.comments_matching(5, "a person is needed"))

    def test_an_issue_closed_while_its_round_runs_gets_no_pull_request(self):
        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return plan()
            # A person closes the issue while the agent works.
            return {**done(), "working": lambda pipe: pipe.gh.close_issue(5, "not_planned")}

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5)])
        self.assertEqual(pipe.played[-1][2], "issue_closed")
        self.assertEqual(pipe.gh.pulls, {})
        self.assertEqual(pipe.gh.issues[5]["state_reason"], "not_planned")
        [run] = pipe.gh.run_comments(5)[-1:]
        self.assertIn("#5 closed while the implement round ran", run["body"])
        self.assertFalse(pipe.gh.comments_matching(5, "a person is needed"))
        # Reopened, it waits for a person: nothing changed since `/nexkit go` started.
        pipe.gh.issues[5]["state"] = "open"
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5)])
        [mention] = pipe.gh.comments_matching(5, "@olivia a person is needed")
        self.assertIn("It closed while `/nexkit go` ran and was reopened", mention["body"])

    def test_a_plan_whose_no_change_is_only_quotes_goes_on_to_its_change(self):
        # An agent wrote the empty string as `""`: the plan's change is still made.
        def script(pipe, inputs):
            return plan(no_change='""') if inputs["command"] == "plan" else done()

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5)])
        self.assertEqual(pipe.played[-1][2], "merged")
        self.assertEqual(pipe.gh.issues[5]["state_reason"], "completed")
        self.assertFalse(pipe.gh.comments_matching(5, "No change needed"))
        self.assertFalse(pipe.gh.comments_matching(5, "no change is needed"))

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

    def test_a_blocked_go_that_recommends_a_split_plans_again_and_splits(self):
        # As in phuongnse/axis#3864: the round recommends splitting the work, which another
        # `/nexkit go` cannot do; a new plan with the answer as its note splits the issue.
        split = "Split the work: file an issue for the stack first, then finish this one"
        parts = [
            {
                "title": "Let the eval stack carry an authoring request",
                "body": "A.",
                "depends_on": [],
            },
            {"title": "Script the authoring eval", "body": "B.", "depends_on": [1]},
        ]

        def script(pipe, inputs):
            number = int(inputs["number"])
            if inputs["command"] == "plan":
                if number == 5 and autopilot.REPLAN_NOTE in inputs["note"]:
                    self.assertIn(split, inputs["note"])
                    return plan(too_large=True, split=parts)
                return plan()
            if number != 5:
                return done()
            question = {
                "question": "The eval stack cannot carry an authoring request yet.",
                "options": [split, "Wait for a person to change the stack"],
                "recommended": 1,
                "closes": 0,
                "replans": [1],
            }
            output = {"status": "blocked", "blocker": "Stack.", "questions": [question]}
            return {"agent": {"status": "blocked", "error": "Stack.", "output": output}}

        pipe = Pipeline(self, config(close_parent_issues=True), script)
        pipe.issue(5)
        played = pipe.play()
        self.assertEqual(played[:3], [("plan", 5), ("go", 5), ("plan", 5)])
        self.assertEqual(played.count(("go", 5)), 1)
        [answered] = pipe.gh.comments_matching(5, "answers for the owner")
        self.assertIn(split, answered["body"])
        self.assertEqual(len(pipe.gh.sub_issues(5)), 2)
        self.assertTrue(all(pipe.gh.issues[n]["state"] == "closed" for n in (5, 6, 7)), played)
        self.assertFalse(pipe.gh.comments_matching(5, "a person is needed"))
        # The round's comment tells a person how to answer with that option.
        [round_] = pipe.gh.comments_matching(5, "Decisions needed")
        self.assertIn(f"- {split} (recommended){state.REPLAN_MARK}", round_["body"])

    def test_a_go_that_blocks_again_after_the_plan_autopilots_answer_started_waits(self):
        widen = "Widen the issue to cover the importer too"

        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return plan()
            question = {
                "question": "The importer has the same bug.",
                "options": [widen, "Leave the importer alone"],
                "recommended": 1,
                "closes": 0,
                "replans": [1],
            }
            output = {"status": "blocked", "blocker": "Importer.", "questions": [question]}
            return {"agent": {"status": "blocked", "error": "Importer.", "output": output}}

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        # Answered once in a row: the plan it started is part of that answer's round.
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5), ("plan", 5), ("go", 5)])
        self.assertEqual(len(pipe.gh.comments_matching(5, "answers for the owner")), 1)
        self.assertEqual(len(pipe.gh.comments_matching(5, "@olivia a person is needed")), 1)

    def test_a_fix_round_that_recommends_a_new_plan_waits_for_a_person(self):
        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return plan()
            if inputs["command"] == "go":
                return done(checks=False)
            question = {
                "question": "The failing test belongs to another feature.",
                "options": ["Split that feature into its own issue", "Fix the test here"],
                "recommended": 1,
                "replans": [1],
            }
            output = {"status": "blocked", "blocker": "Scope.", "questions": [question]}
            return {"agent": {"status": "blocked", "error": "Scope.", "output": output}}

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5), ("fix", 6)])
        self.assertFalse(pipe.gh.comments_matching(6, "answers for the owner"))
        [mention] = pipe.gh.comments_matching(6, "@olivia a person is needed")
        self.assertIn(autopilot.REPLAN_IN_PULL_DO, mention["body"])
        self.assertFalse(pipe.gh.comments_matching(5, "@olivia a person is needed"))

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
        self.assertTrue(pipe.gh.comments_matching(5, "Every sub-issue of this parent issue"))

    def test_a_parent_that_was_not_closed_after_its_last_part_is_closed_by_a_sweep(self):
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
        # GitHub refuses the close that follows the last part's merge, once.
        real, refused = pipe.gh.close_issue, []

        def close_issue(number, reason="completed"):
            if number == 5 and not refused:
                refused.append(number)
                raise GitHubError(502, "Bad Gateway")
            return real(number, reason)

        pipe.gh.close_issue = close_issue
        played = pipe.play()
        self.assertEqual(refused, [5])
        self.assertTrue(all(pipe.gh.issues[n]["state"] == "closed" for n in (5, 6, 7)), played)
        self.assertEqual(len(pipe.gh.comments_matching(5, "<!-- nexkit:parent-closed -->")), 1)
        self.assertFalse(pipe.gh.comments_matching(5, "a person is needed"))

    def test_a_plan_too_large_without_parts_plans_once_more_then_waits(self):
        def script(pipe, inputs):
            return plan(too_large=True)

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5), ("plan", 5)])
        notes = [
            d["inputs"]["note"] for d in pipe.gh.dispatches if d["inputs"]["command"] == "plan"
        ]
        self.assertEqual(notes, ["", autopilot.PARTS_NOTE])
        self.assertEqual(len(pipe.gh.comments_matching(5, "@olivia a person is needed")), 1)
        # When the plan asked for again suggests parts, each part is carried through.
        parts = [
            {"title": "First part", "body": "A.", "depends_on": []},
            {"title": "Second part", "body": "B.", "depends_on": [1]},
        ]
        plans = itertools.count()

        def splits(pipe, inputs):
            if inputs["command"] == "plan" and inputs["number"] == "5":
                return plan(too_large=True, split=parts if next(plans) else ())
            return plan() if inputs["command"] == "plan" else done()

        pipe = Pipeline(self, config(close_parent_issues=True), splits)
        pipe.issue(5)
        played = pipe.play()
        self.assertEqual(played[:3], [("plan", 5), ("plan", 5), ("plan", 6)])
        self.assertTrue(all(pipe.gh.issues[n]["state"] == "closed" for n in (5, 6, 7)), played)
        self.assertFalse(pipe.gh.comments_matching(5, "a person is needed"))

    def test_a_split_with_too_many_parts_plans_again_for_fewer_and_never_files_it_cut(self):
        many = [
            {"title": f"Part {n}", "body": f"Do {n}.", "depends_on": []}
            for n in range(1, state.MAX_PARTS + 3)
        ]
        fewer = many[:2]
        plans = itertools.count()

        def script(pipe, inputs):
            if inputs["command"] == "plan" and inputs["number"] == "5":
                return plan(too_large=True, split=many if next(plans) == 0 else fewer)
            return plan() if inputs["command"] == "plan" else done()

        pipe = Pipeline(self, config(close_parent_issues=True), script)
        pipe.issue(5)
        played = pipe.play()
        self.assertEqual(played[:3], [("plan", 5), ("plan", 5), ("plan", 6)])
        notes = [
            d["inputs"]["note"]
            for d in pipe.gh.dispatches
            if d["inputs"]["number"] == "5" and d["inputs"]["command"] != "step"
        ]
        note = autopilot.FEWER_NOTE.format(parts=len(many), most=state.MAX_PARTS)
        self.assertEqual(notes, ["", note])
        self.assertEqual(issues(pipe), [5, 6, 7])  # the long split filed nothing
        self.assertTrue(all(pipe.gh.issues[n]["state"] == "closed" for n in (5, 6, 7)), played)
        self.assertFalse(pipe.gh.comments_matching(5, "a person is needed"))
        # A plan that has too many parts again waits for a person, once.
        pipe = Pipeline(self, config(), lambda pipe, inputs: plan(too_large=True, split=many))
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5), ("plan", 5)])
        self.assertEqual(issues(pipe), [5])
        self.assertEqual(len(pipe.gh.comments_matching(5, "@olivia a person is needed")), 1)

    def test_a_parent_missing_parts_of_its_plan_waits_when_its_sub_issues_close(self):
        parts = [{"title": f"Part {n}", "body": f"Do {n}.", "depends_on": []} for n in (1, 2, 3)]

        def script(pipe, inputs):
            if inputs["command"] == "plan":
                number = int(inputs["number"])
                return plan(too_large=True, split=parts) if number == 5 else plan()
            return done()

        pipe = Pipeline(self, config(close_parent_issues=True), script)
        pipe.issue(5)
        # GitHub refuses the third part: the split stops after two.
        real = pipe.gh.create_issue

        def create_issue(title, *args, **kw):
            if title == "Part 3":
                raise GitHubError(422, "Validation Failed")
            return real(title, *args, **kw)

        pipe.gh.create_issue = create_issue
        played = pipe.play()
        self.assertEqual(issues(pipe), [5, 6, 7])
        self.assertTrue(all(pipe.gh.issues[n]["state"] == "closed" for n in (6, 7)), played)
        self.assertEqual(pipe.gh.issues[5]["state"], "open")
        self.assertFalse(pipe.gh.comments_matching(5, "<!-- nexkit:parent-closed -->"))
        self.assertEqual(len(pipe.gh.comments_matching(5, "<!-- nexkit:parent-unfiled -->")), 1)
        mention = pipe.gh.comments_matching(5, "@olivia a person is needed")
        self.assertEqual(len(mention), 1)
        self.assertIn("Its plan lists 3 parts, but it has 2 sub-issues.", mention[0]["body"])

    def parts_of_a_parent(self, cfg):
        """The parent 1 with the completed part 2 and the open part 5, imported into its
        state, and the part 5 closed as completed after it."""
        gh = FakeGitHub()
        gh.add_milestone(1)
        gh.add_issue(1, title="Epic", milestone=1)
        gh.add_issue(2, title="Part", state="closed", reason="completed", milestone=1)
        gh.add_issue(5, title="Part", milestone=1)
        gh.parents.update({2: 1, 5: 1})
        self.assertTrue(host.store_import(gh, cfg, 1))
        self.assertEqual(item_state.read(gh, 1)[1]["state"], "split")
        gh.close_issue(5, "completed")
        return gh

    def close_a_part(self, gh, cfg):
        """The `issues: closed` run of the part 5, as the part's group runs it, and the `step`
        run that it sent into the parent's group. Returns the calls that the first made to
        `item_state.apply` and `item_state.write`, and the lines of both."""
        calls = []

        def spied(real):
            def spy(client, number, *args, **kwargs):
                calls.append(number)
                return real(client, number, *args, **kwargs)

            return spy

        before = copy.deepcopy(gh.issue_comments)
        decision = {"action": "maintain", "task": "parents", "issue": 5}
        with (
            mock.patch.object(item_state, "apply", spied(item_state.apply)),
            mock.patch.object(item_state, "write", spied(item_state.write)),
        ):
            first = maintain(gh, decision, cfg, workflow="nexkit.yml", ref="main")
        # The part's run never stores the parent's state.
        self.assertNotIn(1, calls)
        self.assertEqual(
            [c for c in gh.issue_comments[1] if "nexkit:item-state" in c["body"]],
            [c for c in before[1] if "nexkit:item-state" in c["body"]],
        )
        [step] = [d["inputs"] for d in gh.dispatches if d["inputs"]["command"] == "step"]
        self.assertEqual((step["number"], step["note"]), ("1", "part 5"))
        self.assertEqual(queue_item(gh, "workflow_dispatch", {"inputs": step}), 1)
        self.assertEqual(item_state.read(gh, 1)[1]["revision"], 1)
        from tests.support import play_steps

        return first, play_steps(gh, cfg)

    def test_the_last_part_s_close_ends_its_parent_through_the_parent_s_stored_state(self):
        cfg = config(close_parent_issues=True)
        gh = self.parts_of_a_parent(cfg)
        first, played = self.close_a_part(gh, cfg)
        self.assertIn("Closed #1 as completed: all its sub-issues are closed.", first)
        self.assertIn("Sent the end of #5 to #1's queue.", first)
        # The step run of the parent's group stores the end, and writes nothing else on GitHub.
        self.assertEqual(played[0], ["Recorded the end of #5 in #1's state: close."])
        _, doc = item_state.read(gh, 1)
        # The end is revision 2 and the result of the parent's `close` revision 3: the parent is
        # closed, so no `close` waits in its outbox.
        self.assertEqual((doc["revision"], doc["state"]), (3, "accepted"))
        self.assertIn("part-1-5", doc["recentEvents"])
        self.assertFalse(any(action["kind"] == "close" for action in doc["pending"]))
        self.assertEqual(doc["pending"], [])
        self.assertEqual(gh.issues[1]["state"], "closed")
        self.assertEqual(len(gh.comments_matching(1, "<!-- nexkit:parent-closed -->")), 1)

    def test_without_close_parent_issues_a_parent_s_stored_end_waits_for_a_person(self):
        cfg = config(close_parent_issues=False)
        gh = self.parts_of_a_parent(cfg)
        first, played = self.close_a_part(gh, cfg)
        self.assertNotIn("Closed #1", " ".join(first))
        _, doc = item_state.read(gh, 1)
        self.assertEqual((doc["revision"], doc["state"]), (2, "split"))
        self.assertIn("part-1-5", doc["recentEvents"])
        self.assertEqual(doc["attention"]["reason"], "splitIncomplete")
        self.assertEqual(gh.issues[1]["state"], "open")

    def test_a_stopped_go_plans_again_once_a_person_answers_or_its_blocker_closes(self):
        def script():
            gos = itertools.count()

            def play(pipe, inputs):
                if inputs["command"] == "plan":
                    return plan()
                # The first go of #5 asks to wait for another issue, with no recommendation.
                if inputs["number"] == "5" and next(gos) < 1:
                    return blocked(recommended=0)
                return done()

            return play

        pipe = Pipeline(self, config(), script())
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5)])
        # Nothing changed since the stop: hours of sweeps start nothing.
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5)])
        pipe.gh.human_comment(5, "The artifacts are in the release; use those.")
        played = pipe.play()
        self.assertEqual(played, [("plan", 5), ("go", 5), ("plan", 5), ("go", 5)])
        self.assertEqual(pipe.gh.issues[5]["state"], "closed")
        # The same stop, after which a person marks the issue it waits for as its blocker.
        pipe = Pipeline(self, config(), script())
        comment = pipe.gh.comment

        def stamped(number, body):
            made = comment(number, body)
            made["created_at"] = autopilot.iso(datetime.now(autopilot.UTC))
            return made

        pipe.gh.comment = stamped
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5)])
        pipe.issue(7)
        pipe.gh.blockers[5] = [7]
        played = pipe.play()
        self.assertEqual(played[2:], [("plan", 7), ("go", 7), ("plan", 5), ("go", 5)])
        self.assertTrue(all(pipe.gh.issues[n]["state"] == "closed" for n in (5, 7)))

    def test_a_go_that_ended_in_an_agent_error_plans_again_only_after_a_comment(self):
        def script():
            gos = itertools.count()

            def play(pipe, inputs):
                if inputs["command"] == "plan":
                    return plan()
                # The first go of #5 ends in an agent error.
                if inputs["number"] == "5" and next(gos) < 1:
                    return error()
                return done()

            return play

        pipe = Pipeline(self, config(), script())
        pipe.issue(5)
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5)])
        # Nothing changed since the error: hours of sweeps start nothing, and the issue stays open.
        self.assertEqual(pipe.play(), [("plan", 5), ("go", 5)])
        self.assertEqual(pipe.gh.issues[5]["state"], "open")
        pipe.gh.human_comment(5, "The cause is fixed; try again.")
        played = pipe.play()
        self.assertEqual(played, [("plan", 5), ("go", 5), ("plan", 5), ("go", 5)])
        self.assertEqual(pipe.gh.issues[5]["state"], "closed")

    def test_the_null_scope_runs_two_milestones_at_once_with_a_blocker_across_them(self):
        # Issue 6 keeps failing its checks, so it waits for a person; the others go on.
        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return plan()
            pull = pipe.gh.pulls.get(int(inputs["number"]))
            number = issue_from_branch(pull["head"]["ref"]) if pull else int(inputs["number"])
            return done(checks=number != 6)

        pipe = Pipeline(self, config(autopilot={"scope": None}), script)
        pipe.gh.add_milestone(2)
        pipe.issue(5)
        pipe.gh.add_issue(6, title="Issue 6", milestone=2)
        pipe.gh.add_issue(7, title="Issue 7", milestone=2)
        pipe.gh.add_issue(8, title="Issue 8")
        pipe.gh.blockers[7] = [5]  # milestone 2 waits for milestone 1
        played = pipe.play()
        self.assertEqual(played[:3], [("plan", 5), ("plan", 6), ("plan", 8)])
        self.assertLess(played.index(("go", 5)), played.index(("plan", 7)))
        states = {n: pipe.gh.issues[n]["state"] for n in (5, 6, 7, 8)}
        self.assertEqual(states, {5: "closed", 6: "open", 7: "closed", 8: "closed"})
        [note] = pipe.gh.comments_matching(5, autopilot.DONE)
        self.assertIn("@olivia NexKit autopilot finished milestone M1", note["body"])
        self.assertFalse(any(pipe.gh.comments_matching(n, autopilot.DONE) for n in (7, 8)))

    def test_a_flaky_run_is_recorded_once_however_many_issues_come_after_it(self):
        def script(pipe, inputs):
            return plan() if inputs["command"] == "plan" else done()

        pipe = Pipeline(self, config(merge={"after_workflows": ["ci.yml"]}), script)
        pipe.issue(5)
        # A run of `ci.yml` on `main` that passed when run again.
        pipe.gh.workflow_history["ci.yml"] = [
            {
                "id": 50,
                "head_branch": "main",
                "head_repository": {"full_name": "acme/app"},
                "event": "push",
                "head_sha": "e" * 40,
                "status": "completed",
                "conclusion": "success",
                "run_attempt": 2,
                "html_url": "https://github.com/acme/app/actions/runs/50",
                "created_at": autopilot.iso(datetime.now(autopilot.UTC)),
            }
        ]
        pipe.gh.attempts[(50, 1)] = "failure"
        pipe.gh.jobs[50] = [
            {
                "id": 501,
                "name": "build",
                "conclusion": "failure",
                "steps": [{"name": "Restore", "conclusion": "failure"}],
            }
        ]
        pipe.gh.jobs[(50, 2)] = [{"id": 502, "name": "build", "conclusion": "success"}]
        pipe.play()
        [flaky] = [i for i in pipe.gh.issues.values() if autopilot.flaky_workflow(i)]
        self.assertEqual(flaky["state"], "closed")
        # Then NexKit opens more than a hundred other issues and pull requests.
        for number in range(110):
            other = pipe.gh.create_issue(f"Part {number}", "A part.")
            pipe.gh.close_issue(other["number"])
            other["updated_at"] = autopilot.iso(datetime.now(autopilot.UTC) - timedelta(days=2))
        for _ in range(10):
            pull = pipe.gh.create_pull("x", "nexkit/issue-1", "main", "")
            pipe.gh.close_pull(pull["number"])
        before = len(pipe.gh.issues)
        records = len(pipe.gh.comments(flaky["number"]))
        pipe.sweep()
        self.assertEqual(len(pipe.gh.issues), before)
        self.assertEqual(len(pipe.gh.comments(flaky["number"])), records)

    def test_pull_requests_wait_while_main_fails_the_same_way_and_go_on_once_it_is_fixed(self):
        runs = itertools.count(60)

        def ci(pipe, workflow):
            # CI that NexKit starts after a merge passes: the merge fixed `main`.
            run = {
                "id": next(runs),
                "head_branch": "main",
                "head_repository": {"full_name": "acme/app"},
                "event": "workflow_dispatch",
                "status": "completed",
                "conclusion": "success",
                "run_attempt": 1,
                "created_at": autopilot.iso(datetime.now(autopilot.UTC)),
            }
            pipe.gh.workflow_history[workflow].append(run)

        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return plan()
            # Every implement round fails in `make test`: in the test that fails on `main`,
            # except issue 8's, which breaks a test of its own. The fix rounds pass.
            found = done(checks=inputs["command"] == "fix")
            if inputs["command"] != "fix":
                own = int(inputs["number"]) == 8
                test = "cart.test.ts::empties the cart" if own else "orders.test.ts::adds tax"
                found["checks"][0]["failed_tests"] = [test]
            return found

        cfg = config(merge={"after_workflows": ["ci.yml"]})
        pipe = Pipeline(self, cfg, script, workflow=ci)
        # NexKit's comments carry the time they are made: issue 8's fix round, which its
        # first round started, is still queued when the bug issue's merge starts a sweep,
        # and only a round queued for hours is lost.
        comment = pipe.gh.comment

        def stamped(number, body):
            made = comment(number, body)
            made["created_at"] = autopilot.iso(datetime.now(autopilot.UTC))
            return made

        pipe.gh.comment = stamped
        for number in (5, 6, 7, 8):
            pipe.issue(number)
        red_main(pipe.gh)
        played = pipe.play()
        [bug] = [n for n, i in pipe.gh.issues.items() if autopilot.ci_workflow(i)]
        pulls = {issue_from_branch(p["head"]["ref"]): n for n, p in pipe.gh.pulls.items()}
        outcomes = {(c, n): o for c, n, o in pipe.played if c != "plan"}
        # The bug issue is planned first, and its own pull request gets the usual round.
        self.assertEqual(played[0], ("plan", bug))
        self.assertEqual(outcomes[("go", bug)], "auto_fix")
        for number in (5, 6, 7):
            # Each waited, used no round while it did, then got one round that merged.
            self.assertEqual(outcomes[("go", number)], "ci_waiting")
            self.assertEqual(outcomes[("fix", pulls[number])], "merged")
            self.assertEqual(read_state(pipe.gh.comments(pulls[number]))[1]["auto_fixes"], 1)
            triggers = [run["trigger"] for _, run in run_comments(pipe.gh.comments(pulls[number]))]
            self.assertEqual(triggers, ["implement (autopilot)", "fix (auto)"])
        self.assertLess(played.index(("fix", pulls[bug])), played.index(("fix", pulls[5])))
        # Issue 8's failure is its own, in the same script: it gets its round at once,
        # not after the wait the others get theirs after.
        self.assertEqual(outcomes[("go", 8)], "auto_fix")
        self.assertEqual(outcomes[("fix", pulls[8])], "merged")
        self.assertLess(played.index(("fix", pulls[8])), played.index(("fix", pulls[5])))
        self.assertTrue(all(pipe.gh.issues[n]["state"] == "closed" for n in (5, 6, 7, 8, bug)))
        for number in (5, 6, 7, 8, bug, *pulls.values()):
            self.assertFalse(pipe.gh.comments_matching(number, "a person is needed"))

    def test_the_bug_issue_starts_when_pull_requests_waiting_for_it_fill_max_parallel(self):
        self.bug_issue_at_max_parallel(split=False)

    def test_the_parts_of_that_bug_issue_start_too(self):
        self.bug_issue_at_max_parallel(split=True)

    def bug_issue_at_max_parallel(self, split):
        """Three issues fill `max_parallel` and their pull requests wait for the bug issue
        about `main`. It starts all the same, and so do its parts when its plan is split."""
        runs = itertools.count(60)
        parts = [
            {"title": "First part", "body": "A.", "depends_on": []},
            {"title": "Second part", "body": "B.", "depends_on": [1]},
        ]

        def ci(pipe, workflow):
            # CI that NexKit starts after a merge passes: the merge fixed `main`.
            run = {
                "id": next(runs),
                "head_branch": "main",
                "head_repository": {"full_name": "acme/app"},
                "event": "workflow_dispatch",
                "status": "completed",
                "conclusion": "success",
                "run_attempt": 1,
                "created_at": autopilot.iso(datetime.now(autopilot.UTC)),
            }
            pipe.gh.workflow_history[workflow].append(run)

        def turn_main_red(pipe):
            # While the first implement agent works, `ci.yml` fails on `main` and a sweep
            # opens its bug issue. The three issues in progress fill `max_parallel`.
            if "ci.yml" not in pipe.gh.workflow_history:
                red_main(pipe.gh)
                pipe.sweep()

        def script(pipe, inputs):
            if inputs["command"] == "plan":
                bug = autopilot.ci_workflow(pipe.gh.issues[int(inputs["number"])])
                return plan(too_large=True, split=parts) if split and bug else plan()
            # Every implement round fails in the test that fails on `main`; the fix rounds
            # pass.
            found = done(checks=inputs["command"] == "fix")
            if inputs["command"] != "fix":
                found["checks"][0]["failed_tests"] = ["orders.test.ts::adds tax"]
                found["working"] = turn_main_red
            return found

        cfg = config(
            merge={"after_workflows": ["ci.yml"]},
            autopilot={"max_parallel": 3},
            close_parent_issues=True,
        )
        pipe = Pipeline(self, cfg, script, workflow=ci)
        for number in (5, 6, 7):
            pipe.issue(number)
        played = pipe.play()
        [bug] = [n for n, i in pipe.gh.issues.items() if autopilot.ci_workflow(i)]
        pulls = {issue_from_branch(p["head"]["ref"]): n for n, p in pipe.gh.pulls.items()}
        outcomes = {(c, n): o for c, n, o in pipe.played if c != "plan"}
        for number in (5, 6, 7):
            self.assertEqual(outcomes[("go", number)], "ci_waiting")
        # The pull requests waiting for it hold every place, and the bug issue starts all
        # the same; once it is merged, they get their round.
        self.assertLess(played.index(("plan", bug)), played.index(("fix", pulls[5])))
        fixing = [n for n in pipe.gh.issues if pipe.gh.parent_issue(n)] if split else [bug]
        self.assertEqual(len(fixing), 2 if split else 1)
        # The first part, or the bug issue itself, fixes `main`, and that ends the wait.
        self.assertLess(played.index(("go", min(fixing))), played.index(("fix", pulls[5])))
        for number in fixing:
            self.assertEqual(outcomes[("fix", pulls[number])], "merged")
        for number in (5, 6, 7):
            self.assertEqual(outcomes[("fix", pulls[number])], "merged")
        done_ = (5, 6, 7, bug, *fixing)
        self.assertTrue(all(pipe.gh.issues[n]["state"] == "closed" for n in done_), played)
        for number in (*done_, *pulls.values()):
            self.assertFalse(pipe.gh.comments_matching(number, "a person is needed"))

    def test_a_workflow_that_runs_every_third_merge_starts_at_the_third(self):
        runs = itertools.count(60)

        def ci(pipe, workflow):
            # A run of `ci.yml` tests the tip of `main`, and passes before the next merge.
            run_id = next(runs)
            pipe.gh.workflow_history.setdefault(workflow, []).append(
                {
                    "id": run_id,
                    "head_branch": "main",
                    "head_repository": {"full_name": "acme/app"},
                    "event": "workflow_dispatch",
                    "head_sha": pipe.gh.history["main"][-1],
                    "status": "completed",
                    "conclusion": "success",
                    "run_attempt": 1,
                    "html_url": f"https://github.com/acme/app/actions/runs/{run_id}",
                    "created_at": autopilot.iso(datetime.now(autopilot.UTC)),
                }
            )

        def script(pipe, inputs):
            return plan() if inputs["command"] == "plan" else done()

        cfg = config(merge={"after_workflows": ["smoke.yml", {"workflow": "ci.yml", "every": 3}]})
        pipe = Pipeline(self, cfg, script)
        dispatch = pipe.gh.dispatch

        def dispatched(workflow, ref, inputs, run_details=False):
            found = dispatch(workflow, ref, inputs, run_details)
            if workflow == "ci.yml":
                ci(pipe, workflow)
            return found

        pipe.gh.dispatch = dispatched
        # `ci.yml` last ran on `main` for a person's push, at its tip.
        pipe.gh.history["main"] = ["0" * 40]
        ci(pipe, "ci.yml")
        pipe.gh.workflow_history["ci.yml"][0]["event"] = "push"
        for number in range(5, 12):
            pipe.issue(number)
        pipe.play()
        self.assertTrue(all(pipe.gh.issues[n]["state"] == "closed" for n in range(5, 12)))
        # Seven merges: `smoke.yml` starts after each, `ci.yml` after the third and sixth.
        self.assertEqual(len(pipe.gh.history["main"]), 8)
        merged = [
            [run for _, run in run_comments(pipe.gh.comments(number)) if "round" in run][-1]
            for number, _, _ in pipe.gh.merged
        ]
        started = [[r["workflow"] for r in run.get("after_merge", [])] for run in merged]
        self.assertEqual(
            started,
            [["smoke.yml"], ["smoke.yml"], ["smoke.yml", "ci.yml"]] * 2 + [["smoke.yml"]],
        )
        self.assertEqual(len(pipe.gh.workflow_history["ci.yml"]), 3)
        last = pipe.gh.merged[-1][0]
        self.assertTrue(pipe.gh.comments_matching(last, "`ci.yml` waits: 1 of 3 commits"))

    def test_a_watched_workflow_is_never_started_but_its_failures_on_main_are_handled(self):
        # `ci.yml` runs on `main` on a schedule only: NexKit watches it, never starts it.
        def script(pipe, inputs):
            if inputs["command"] == "plan":
                return plan()
            # Issue 5's implement round fails in the test that fails on `main`; the fix
            # rounds pass.
            found = done(checks=inputs["command"] == "fix" or int(inputs["number"]) != 5)
            if not found["checks"][0]["passed"]:
                found["checks"][0]["failed_tests"] = ["orders.test.ts::adds tax"]
            return found

        cfg = config(autopilot={"watch_workflows": ["ci.yml"]})
        self.assertEqual(cfg["merge"]["after_workflows"], [])
        pipe = Pipeline(self, cfg, script)
        pipe.issue(5)
        # The scheduled run failed in the step that runs `make test`.
        run = {
            "id": 50,
            "head_branch": "main",
            "head_repository": {"full_name": "acme/app"},
            "event": "schedule",
            "head_sha": "e" * 40,
            "status": "completed",
            "conclusion": "failure",
            "run_attempt": 1,
            "html_url": "https://github.com/acme/app/actions/runs/50",
            "created_at": autopilot.iso(datetime.now(autopilot.UTC)),
        }
        pipe.gh.workflow_history["ci.yml"] = [run]
        pipe.gh.jobs[50] = [{"id": 501, "name": "Unit tests", "conclusion": "failure"}]
        pipe.gh.logs[501] = (
            b"##[group]Run make test\n##[endgroup]\nFAIL\n"
            b"##[error]Process completed with exit code 1.\n"
        )
        pipe.gh.annotations[501] = [
            {
                "annotation_level": "failure",
                "title": "Orders > adds tax",
                "path": "web/src/orders.test.ts",
                "message": "expected 2 to be 3",
            }
        ]
        rerun = pipe.gh.rerun_failed_jobs

        def failed_again(run_id):
            # The one more try of its failed jobs fails the same way.
            rerun(run_id)
            run["run_attempt"] += 1

        pipe.gh.rerun_failed_jobs = failed_again
        played = pipe.play()
        # One more try of the failed jobs, then a bug issue, planned before the round of
        # issue 5 runs.
        self.assertEqual(pipe.gh.reruns, [50])
        [bug] = [n for n, i in pipe.gh.issues.items() if autopilot.ci_workflow(i)]
        self.assertLess(played.index(("plan", bug)), played.index(("go", 5)))
        pulls = {issue_from_branch(p["head"]["ref"]): n for n, p in pipe.gh.pulls.items()}
        outcomes = {(c, n): o for c, n, o in pipe.played if c != "plan"}
        # Issue 5's pull request waited for the bug issue, then got its round after it.
        self.assertEqual(outcomes[("go", 5)], "ci_waiting")
        self.assertEqual(outcomes[("fix", pulls[5])], "merged")
        self.assertLess(played.index(("go", bug)), played.index(("fix", pulls[5])))
        self.assertTrue(all(pipe.gh.issues[n]["state"] == "closed" for n in (5, bug)))
        # Two merges, and NexKit started no workflow after either.
        self.assertEqual(len(pipe.gh.merged), 2)
        self.assertEqual({d["workflow"] for d in pipe.gh.dispatches}, {"nexkit.yml"})

    def test_a_run_whose_writes_all_lose_fails_and_the_next_run_answers_the_event_once(self):
        group = ItemGroup(self)
        asked = group.post("/nexkit plan Keep it short")
        with group.losing():
            group.queue("first")
        # The run fails, and stores nothing of the event, which stays at its source.
        self.assertEqual(
            (group.failed, group.answered), (["first"], [f"comment-{asked['id']}"] * 4)
        )
        self.assertNotIn(f"comment-{asked['id']}", group.state()["recentEvents"])
        self.assertEqual(group.state()["state"], "new")
        self.assertIsNone(group.cursor())
        # A later comment starts the next run, which answers the event once, and the new one.
        remark = group.post("Thanks, that helps", login="bob")
        group.answered.clear()
        group.queue("second")
        self.assertEqual(group.failed, ["first"])
        self.assertEqual(group.answered, [f"comment-{asked['id']}", f"comment-{remark['id']}"])
        self.assertEqual(group.state()["state"], "planning")
        self.assertEqual(group.state()["recentEvents"].count(f"comment-{asked['id']}"), 1)
        self.assertEqual(group.cursor()[1], f"comment-{remark['id']}")
        # And a third run finds nothing.
        group.finish()
        group.answered.clear()
        group.queue("third")
        self.assertEqual(group.answered, [])

    def test_a_command_whose_run_a_newer_one_replaced_is_answered_by_the_run_that_ran(self):
        group = ItemGroup(self)
        group.queue("running")
        # While it runs, two commands are posted, and the second one's run replaces the first's.
        self.assertEqual(group.answered, [])
        first = group.post("/nexkit plan Start with the tests")
        group.queue("first")
        second = group.post("/nexkit plan Start with the docs")
        group.queue("second")
        self.assertEqual((group.replaced, group.waiting), (["first"], "second"))
        group.finish()
        # The run that came before both took neither, and the one that ran after it answered
        # both, in the order they were posted.
        ids = [f"comment-{first['id']}", f"comment-{second['id']}"]
        self.assertEqual(group.answered, ids)
        # Each command is answered once, and by the run that ran, not by the one replaced.
        self.assertEqual([group.answered.count(i) for i in ids], [1, 1])
        self.assertEqual(group.failed, [])
        state = group.state()
        # The first command's effect is stored: it started the plan, so the second one, a
        # plan while one runs, changed nothing.
        self.assertEqual(state["state"], "planning")
        self.assertEqual(state["recentEvents"][-1], ids[0])
        self.assertEqual(group.cursor()[1], ids[1])

    # -- the first state of an item ------------------------------------------------------

    def stored_import(self, pipe, number=5, pull=None):
        """Run `take_run` on `pipe` and check that it stored one state comment, at revision 1,
        which holds what `first_state.import_item` makes of the item now. Returns the document."""
        out = pipe.take_run(number)
        self.assertIn(f"Stored the first state of #{number}", out)
        [comment] = pipe.state_comments(number)
        stored = item_state.find([comment])[1]
        expected = first_state.import_item(pipe.gh, pipe.cfg, number, pull, now=IMPORTED_AT)
        self.assertEqual(stored, expected)
        self.assertEqual(stored["revision"], 1)
        # A second run finds it and stores nothing more.
        self.assertNotIn("Stored the first state", pipe.take_run(number))
        self.assertEqual(pipe.state_comments(number), [comment])
        return stored

    def same_item(self, imported, plain, issue_state, skew=timedelta(0)):
        """`imported`, which stored an import in the middle of its work, plays on as `plain`,
        which stored none: the same runs, merges, comments of the issue and end. Call
        `freeze_clock` before building either pipeline, so that no wall-clock stamp, such as
        `waiting_since` or an autopilot record's `at`, decides the comparison. `plain` plays
        with the frozen clock `skew` behind, as a slow runner plays it in another second."""
        self.assertTrue(
            getattr(self, "clock_frozen", False),
            "call self.freeze_clock() before building the pipelines that same_item compares",
        )
        frozen = self.frozen_at - skew

        class Behind(datetime):
            @classmethod
            def now(cls, tz=None):
                return frozen

        played = imported.play()
        with mock.patch.object(autopilot, "datetime", Behind):
            self.assertEqual(played, plain.play())
        self.assertEqual(imported.played, plain.played)
        self.assertEqual(imported.gh.merged, plain.gh.merged)
        self.assertEqual(imported.gh.issues[5]["state"], issue_state)
        self.assertEqual(plain.gh.issues[5]["state"], issue_state)

        self.assertEqual(people_read(imported), people_read(plain))
        self.assertEqual(
            collect(imported.gh, imported.cfg, workers=1)["needs_person"],
            collect(plain.gh, plain.cfg, workers=1)["needs_person"],
        )

    def test_what_people_read_sets_aside_the_times_of_records(self):
        def body(at, basis):
            record = {"basis": basis, "command": "plan", "at": at}
            return f"Text at 2026-01-01T00:00:00Z\n{autopilot.record_marker(record)}"

        first = body("2026-01-01T00:00:00Z", [1, 0])
        self.assertNotEqual(first, body("2026-01-01T00:00:01Z", [1, 0]))
        self.assertEqual(timeless(first), timeless(body("2026-01-01T00:00:01Z", [1, 0])))
        self.assertNotEqual(timeless(first), timeless(body("2026-01-01T00:00:00Z", [2, 0])))

    def test_a_plan_waiting_for_approval_is_imported_and_waits_as_before(self):
        self.freeze_clock()

        def script(pipe, inputs):
            return plan(questions=[{"question": "Which?", "options": ["a", "b"]}])

        def built():
            pipe = Pipeline(self, config(autopilot={"answer": "never"}), script)
            pipe.issue(5)
            autopilot.request(pipe.gh, "nexkit.yml", "main")
            pipe.drain()
            self.assertEqual([c for c, *_ in pipe.played], ["plan"])
            return pipe

        imported, plain = built(), built()
        stored = self.stored_import(imported)
        self.assertEqual(stored["itemKind"], "change")
        self.same_item(imported, plain, "open")
        self.assertTrue(imported.gh.comments_matching(5, "a person is needed"))

    def test_an_imported_plan_that_a_comment_follows_plays_on_and_stores_the_answer(self):
        self.freeze_clock()

        def script(pipe, inputs):
            return plan(questions=[{"question": "Which?", "options": ["a", "b"]}])

        def built():
            pipe = Pipeline(self, config(autopilot={"answer": "never"}), script)
            pipe.issue(5)
            autopilot.request(pipe.gh, "nexkit.yml", "main")
            pipe.drain()
            self.assertEqual([c for c, *_ in pipe.played], ["plan"])
            return pipe

        imported, plain = built(), built()
        self.stored_import(imported)
        for pipe in (imported, plain):
            pipe.gh.human_comment(5, "Please keep it small.")
        # Force the records of the two plays a second apart, as a slow runner does.
        self.same_item(imported, plain, "open", skew=timedelta(seconds=1))
        # The runs of the item's group stored their answers past the import's revision, while
        # the item that stored no import still has no state comment.
        [comment] = imported.state_comments(5)
        self.assertGreater(item_state.find([comment])[1]["revision"], 1)
        self.assertEqual(plain.state_comments(5), [])

    def test_a_pull_request_in_its_checks_is_imported_and_carries_on_as_before(self):
        def built(import_there):
            seen = []

            def checking(pipe, pull):
                if import_there and not seen:
                    seen.append(self.stored_import(pipe, 5, pull))

            def script(pipe, inputs):
                if inputs["command"] == "plan":
                    return plan()
                return {**done(), "checking": checking}

            pipe = Pipeline(self, config(), script)
            pipe.issue(5)
            pipe.play()
            return pipe, seen

        imported, seen = built(True)
        plain, _ = built(False)
        # The import was read while the round checked its pull request.
        self.assertEqual(seen[0]["change"]["id"], "6")
        self.assertEqual(imported.played, plain.played)
        self.assertEqual(imported.gh.merged, plain.gh.merged)
        self.assertEqual(imported.gh.issues[5]["state"], "closed")
        self.assertEqual(plain.gh.issues[5]["state"], "closed")
        self.assertEqual(len(imported.state_comments(5)), 1)

    def test_a_rounds_checks_and_review_are_stored_and_the_stored_count_stops_the_rounds(self):
        def built(stored_count):
            seen = {"revisions": [], "heads": [], "events": []}

            def checking(pipe, pull):
                if not seen["revisions"]:
                    self.stored_import(pipe, 5, pull)
                comment, doc = item_state.read(pipe.gh, 5)
                seen["revisions"].append(doc["revision"])
                seen["heads"].append(pipe.gh.pulls[pull]["head"]["sha"])
                seen["events"].append(doc["recentEvents"])
                if len(seen["revisions"]) == 2 and stored_count:
                    # The state says that the rounds are used, as another run of the item's
                    # group may have stored it, while NexKit 6's marker counts one.
                    found = {**doc, "revision": doc["revision"] + 1}
                    found["rounds"] = {**doc["rounds"], "counted": stored_count}
                    self.assertTrue(item_state.write(pipe.gh, 5, found, (comment, doc)))

            def script(pipe, inputs):
                if inputs["command"] == "plan":
                    return plan()
                return {**done(checks=False), "checking": checking}

            pipe = Pipeline(self, config(fix={"max_auto_rounds": 3}), script)
            pipe.issue(5)
            return pipe, pipe.play(), seen

        plain, played, seen = built(0)
        # The import, then each round: its agent's and publish results in one write
        # (`host.round_published`), and its checks and its review, each as its own revision.
        self.assertEqual(seen["revisions"], [1, 4, 7, 10])
        first, head = seen["events"][1], seen["heads"][0]
        self.assertTrue(any(name.startswith(f"publish-6-{head}") for name in first), first)
        self.assertTrue(any(name.startswith(f"checks-6-{head}") for name in first), first)
        self.assertTrue(any(name.startswith(f"round-review-6-{head}") for name in first), first)
        self.assertEqual(played, [("plan", 5), ("go", 5), ("fix", 6), ("fix", 6), ("fix", 6)])
        # The stored count, above the one NexKit 6's marker holds, stops the rounds earlier.
        stored, played, seen = built(3)
        self.assertEqual(played, [("plan", 5), ("go", 5), ("fix", 6)])
        self.assertEqual(stored.played[-1][2], "needs_human")
        self.assertEqual(read_state(stored.gh.comments(6))[1]["auto_fixes"], 1)
        self.assertEqual(stored.gh.issues[5]["state"], "open")
        self.assertEqual(plain.gh.issues[5]["state"], "open")

    def test_a_paused_run_is_imported_and_resumes_as_before(self):
        self.freeze_clock()

        def built():
            pauses = itertools.count()

            def script(pipe, inputs):
                if inputs["command"] == "plan":
                    return plan()
                if inputs["command"] == "go" and next(pauses) < 1:
                    at = autopilot.iso(datetime.now(UTC) + timedelta(minutes=30))
                    limit = "You've hit your session limit"
                    return {
                        "agent": {
                            "status": "paused",
                            "error": f"Claude hit the account's usage limit: {limit}",
                            "limit": limit,
                            "resume_at": at,
                            "reset_known": True,
                        }
                    }
                return done()

            pipe = Pipeline(self, config(), script)
            pipe.issue(5)
            autopilot.request(pipe.gh, "nexkit.yml", "main")
            while pipe.gh.dispatches[pipe.handled]["inputs"]["command"] != "resume":
                pipe.drain_one()
            self.assertEqual(pipe.gh.run_comments(5)[-1]["run"]["status"], "paused")
            return pipe

        imported, plain = built(), built()
        self.stored_import(imported)
        for pipe in (imported, plain):
            pipe.later(timedelta(minutes=31))
        self.same_item(imported, plain, "closed")
        self.assertEqual([c for c, *_ in imported.played].count("go"), 2)

    def freeze_clock(self):
        """Make `datetime.now` read one time in every module of NexKit and in the fake GitHub.
        Two pipelines built and played one after the other then stamp the same time in their
        records, so a slow build cannot make them differ by a second."""
        self.clock_frozen = True
        started = self.frozen_at = datetime.now(UTC)

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return started

        clocks = contextlib.ExitStack()
        self.addCleanup(clocks.close)
        for name, module in list(sys.modules.items()):
            if name.startswith(("nexkit.", "tests.support")) and (
                getattr(module, "datetime", None) is datetime
            ):
                clocks.enter_context(mock.patch.object(module, "datetime", Clock))

    def test_a_merge_waiting_for_required_checks_is_imported_and_merges_as_before(self):
        def script(pipe, inputs):
            return plan() if inputs["command"] == "plan" else done()

        self.freeze_clock()

        def built():
            pipe = Pipeline(self, config(), script)
            pipe.issue(5)
            pipe.gh.required = {"e2e"}
            pipe.gh.merge_error = GitHubError(405, 'Required status check "e2e" is expected.')
            autopilot.request(pipe.gh, "nexkit.yml", "main")
            pipe.drain()
            self.assertEqual([o for *_, o in pipe.played], ["planned", "merge_waiting"])
            return pipe

        imported, plain = built(), built()
        stored = self.stored_import(imported, 5, 6)
        self.assertEqual(stored["change"]["id"], "6")
        for pipe in (imported, plain):
            # The required check passes: the next sweep merges.
            pipe.gh.merge_error = None
            pipe.head(6)
        self.same_item(imported, plain, "closed")
        self.assertEqual(len(imported.gh.merged), 1)

    def test_a_merge_of_an_issue_with_no_parent_is_stored_as_accepted_with_nothing_pending(self):
        def script(pipe, inputs):
            return plan() if inputs["command"] == "plan" else done()

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        pipe.gh.required = {"e2e"}
        pipe.gh.merge_error = GitHubError(405, 'Required status check "e2e" is expected.')
        autopilot.request(pipe.gh, "nexkit.yml", "main")
        pipe.drain()
        self.assertEqual([o for *_, o in pipe.played], ["planned", "merge_waiting"])
        self.stored_import(pipe, 5, 6)
        self.assertIsNone(pipe.gh.parent_issue(5))
        pipe.gh.merge_error = None
        pipe.head(6)
        pipe.play()
        self.assertEqual((len(pipe.gh.merged), pipe.gh.issues[5]["state"]), (1, "closed"))
        # The merge is stored whether or not the issue has a parent, and the close that went
        # with it is reported, so nothing waits in the outbox.
        doc = item_state.read(pipe.gh, 5)[1]
        self.assertEqual((doc["state"], doc["pending"]), ("accepted", []))

    def test_a_merge_waiting_for_required_checks_keeps_its_first_wait_and_ends_for_a_person(self):
        def script(pipe, inputs):
            return plan() if inputs["command"] == "plan" else done()

        def built():
            pipe = Pipeline(self, config(), script)
            pipe.issue(5)
            pipe.gh.required = {"e2e"}
            pipe.gh.merge_error = GitHubError(405, 'Required status check "e2e" is expected.')
            autopilot.request(pipe.gh, "nexkit.yml", "main")
            pipe.drain()
            self.assertEqual([o for *_, o in pipe.played], ["planned", "merge_waiting"])
            return pipe

        def sweep(pipe, hours):
            """A sweep `hours` after the wait began, and the runs it starts."""

            class Later(datetime):
                @classmethod
                def now(cls, tz=None):
                    return datetime.now(tz) + timedelta(hours=hours)

            with mock.patch.object(autopilot, "datetime", Later):
                pipe.sweep()
                pipe.drain()

        stored, plain = built(), built()
        # Force the two items' record times apart, as a slow runner does between two plays.
        plain.later(timedelta(seconds=1))
        self.stored_import(stored, 5, 6)
        first = item_state.read(stored.gh, 5)[1]
        self.assertEqual([wait["for"] for wait in first["waits"]], ["requiredChecks"])
        since = first["checksSince"]["at"]
        revision = first["revision"]
        for hours in (2, 4):
            for pipe in (stored, plain):
                sweep(pipe, hours)
            # A sweep that merges again stores the answer of the engine: the revision grows and
            # the first wait's time stays.
            document = item_state.read(stored.gh, 5)[1]
            self.assertGreater(document["revision"], revision)
            revision = document["revision"]
            self.assertEqual(document["checksSince"]["at"], since)
            self.assertEqual(document["state"], "accepting")
            self.assertEqual(stored.gh.merged, [])
        # More than six hours after the first wait the engine hands the merge to a person.
        for pipe in (stored, plain):
            sweep(pipe, 7)
        document = item_state.read(stored.gh, 5)[1]
        self.assertGreater(document["revision"], revision)
        self.assertEqual(document["state"], "ready")
        self.assertEqual(document["attention"]["reason"], "acceptRefused")
        self.assertEqual(stored.gh.merged, [])
        mentions = [c for n in (5, 6) for c in stored.gh.comments_matching(n, "a person is needed")]
        self.assertEqual(len(mentions), 1)

        # A further sweep after the hand-off waits for the person: the engine's tick changes
        # nothing, the merge is not tried, and no one is mentioned again.
        for pipe in (stored, plain):
            sweep(pipe, 9)
        document = item_state.read(stored.gh, 5)[1]
        self.assertEqual(document["state"], "ready")
        self.assertEqual(document["attention"]["reason"], "acceptRefused")
        self.assertEqual(stored.gh.merged, [])
        mentions = [c for n in (5, 6) for c in stored.gh.comments_matching(n, "a person is needed")]
        self.assertEqual(len(mentions), 1)

        # What people read is what a plain run leaves.
        self.assertEqual(people_read(stored), people_read(plain))
        self.assertEqual(stored.played, plain.played)
        self.assertEqual(stored.gh.issues[5]["state"], plain.gh.issues[5]["state"])

    def test_the_sweep_and_status_only_preview_a_waiting_merge_and_the_step_run_stores_it(self):
        def script(pipe, inputs):
            return plan() if inputs["command"] == "plan" else done()

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        pipe.gh.required = {"e2e"}
        pipe.gh.merge_error = GitHubError(405, 'Required status check "e2e" is expected.')
        autopilot.request(pipe.gh, "nexkit.yml", "main")
        pipe.drain()
        self.stored_import(pipe, 5, 6)
        before = copy.deepcopy(pipe.gh.comments(5)), copy.deepcopy(pipe.gh.comments(6))
        issue = pipe.gh.issue(5)
        # What the sweep and `nexkit status` read decides the tick and stores nothing.
        step = autopilot.assess(pipe.gh, pipe.cfg, issue, autopilot.nexkit_pulls(pipe.gh))
        self.assertEqual(step["state"], "merge")
        autopilot.preview(pipe.gh, pipe.cfg)
        collect(pipe.gh, pipe.cfg, workers=1)
        self.assertEqual((pipe.gh.comments(5), pipe.gh.comments(6)), before)
        self.assertEqual(item_state.read(pipe.gh, 5)[1]["revision"], 1)
        # The `step` run in the issue's group stores the engine's answer to the tick.
        head = pipe.gh.pulls[6]["head"]["sha"]
        with contextlib.redirect_stdout(io.StringIO()):
            autopilot.step(pipe.gh, pipe.cfg, 5, "nexkit.yml", "main")
        document = item_state.read(pipe.gh, 5)[1]
        self.assertGreater(document["revision"], 1)
        # The id carries the revision that the tick was decided on.
        self.assertIn(f"merge-6-{head}-r1", document["recentEvents"])

    def test_a_state_comment_that_someone_else_changed_gets_no_second_one_and_waits_for_a_person(
        self,
    ):
        def script(pipe, inputs):
            return plan() if inputs["command"] == "plan" else done()

        pipe = Pipeline(self, config(), script)
        pipe.issue(5)
        pipe.gh.required = {"e2e"}
        pipe.gh.merge_error = GitHubError(405, 'Required status check "e2e" is expected.')
        autopilot.request(pipe.gh, "nexkit.yml", "main")
        pipe.drain()
        self.stored_import(pipe, 5, 6)
        [comment] = pipe.state_comments(5)
        comment["editors"] = [BOT_LOGIN, "mallory"]
        out = pipe.take_run(5)
        self.assertIn("::warning::", out)
        self.assertIn("@mallory changed a comment of NexKit's", out)
        self.assertEqual(pipe.state_comments(5), [comment])
        needs = collect(pipe.gh, pipe.cfg, workers=1)["needs_person"]
        found = [item for item in needs if autopilot.CHANGED_ENTRY in item["reason"]]
        self.assertTrue(found and all("@mallory" in item["reason"] for item in found), needs)
        # Deleting the comment imports the item again, at revision 1, on the next run.
        pipe.gh.issue_comments[5].remove(comment)
        self.assertIn("Stored the first state of #5", pipe.take_run(5))
        [again] = pipe.state_comments(5)
        self.assertNotEqual(again["id"], comment["id"])
        self.assertEqual(item_state.find([again])[1]["revision"], 1)

    def test_the_first_run_of_an_item_imports_and_a_later_run_answers_a_replaced_command(self):
        group = ItemGroup(self, stored=False)
        self.assertEqual(group.gh.comments(group.NUMBER), [])
        group.queue("first")
        # The first run stored the import at revision 1, before any comment of a person.
        [comment] = group.gh.comments(group.NUMBER)
        self.assertEqual(group.state()["revision"], 1)
        self.assertIsNone(item_state.since(comment))
        self.assertEqual(group.answered, [])
        # While it runs, a command is posted and its run waits, and a remark's run replaces it.
        asked = group.post("/nexkit plan Keep it short")
        group.queue("asked")
        group.post("Thanks, that helps", login="bob")
        group.queue("remark")
        self.assertEqual(group.replaced, ["asked"])
        group.finish()
        self.assertIn(f"comment-{asked['id']}", group.answered)
        self.assertEqual(group.state()["state"], "planning")
        self.assertEqual(len(group.gh.comments(group.NUMBER)), 3)
        # The cursor moved past the command's comment.
        at = asked["created_at"]
        self.assertGreaterEqual(
            item_state.order(group.cursor()), item_state.order((at, f"comment-{asked['id']}"))
        )

    def test_a_plan_command_starts_its_plan_once_and_stores_one_revision(self):
        for stored in (True, False):
            with self.subTest(stored=stored):
                group = ItemGroup(self, stored=stored, routes=True)
                if not stored:
                    group.queue("import")
                    group.finish()
                before = group.state()["revision"]
                asked = group.post("/nexkit plan Keep it short")
                name = f"comment-{asked['id']}"
                group.queue("asked", comment=asked)
                decision = group.decisions["asked"]
                found = (decision["action"], decision["comment_id"])
                self.assertEqual(found, ("plan", asked["id"]))
                # The engine answered it once, when `take` stored its answer, and the state
                # comment moved by that one revision.
                self.assertEqual(group.answered.count(name), 1)
                state = group.state()
                self.assertEqual(state["revision"], before + 1)
                self.assertEqual(state["recentEvents"].count(name), 1)
                self.assertEqual(state["state"], "planning")
                # A remark's run, and a run again of the command, which an earlier run took,
                # start nothing and reply nothing.
                group.finish()
                remark = group.post("Thanks, that helps", login="bob")
                group.queue("remark", comment=remark)
                self.assertEqual(group.decisions["remark"]["action"], "none")
                group.finish()
                group.queue("again", comment=asked)
                again = group.decisions["again"]
                self.assertEqual((again["action"], again.get("reply_to")), ("none", None))
                self.assertEqual(group.state()["recentEvents"].count(name), 1)

    def test_a_command_whose_run_was_replaced_starts_its_stage_in_the_run_that_ran(self):
        group = ItemGroup(self, routes=True)
        group.queue("running")
        asked = group.post("/nexkit plan Keep it short")
        group.queue("asked", comment=asked)
        remark = group.post("Thanks, that helps", login="bob")
        group.queue("remark", comment=remark)
        self.assertEqual((group.replaced, group.waiting), (["asked"], "remark"))
        group.finish()
        # The remark's run, which ran, starts the plan that the command asked for, and the
        # command's comment is the one that gets 👀 (`progress.start` reacts to `comment_id`).
        decision = group.decisions["remark"]
        self.assertEqual((decision["action"], decision["comment_id"]), ("plan", asked["id"]))
        self.assertNotIn("also_reply", decision)
        self.assertEqual(group.state()["state"], "planning")
        self.assertEqual(group.state()["recentEvents"].count(f"comment-{asked['id']}"), 1)
        self.assertNotIn("asked", group.decisions)

    # -- closed items ----------------------------------------------------------------------

    def routed(self, gh, cfg, event):
        """The run of a comment, as `nexkit route` makes it: `cli._take`, then `route` on what
        it took. Returns the decision and the events that reached the engine."""
        real, sent = host.step, []

        def spy(state, one, found):
            sent.append(one)
            return real(state, one, found)

        with mock.patch.object(host, "step", spy), contextlib.redirect_stdout(io.StringIO()):
            taken = cli._take(gh, cfg, "issue_comment", event)
            decision = route(gh, "issue_comment", event, cfg=cfg, taken=taken)
        return decision, sent

    def test_a_plan_command_on_a_closed_issue_is_refused_and_never_reaches_its_state(self):
        group = ItemGroup(self)
        group.gh.close_issue(5)
        before = copy.deepcopy(group.state())
        asked = group.post("/nexkit plan")
        event = {"action": "created", "issue": {"number": 5}, "comment": asked}
        decision, sent = self.routed(group.gh, group.cfg, event)
        # NexKit 6's reply, and nothing to start or dispatch.
        self.assertEqual(
            (decision["action"], decision["reason"], decision["reply_to"]),
            ("none", "The issue is closed", 5),
        )
        self.assertEqual(group.gh.dispatches, [])
        self.assertEqual([one for one in sent if one["kind"] == "requested"], [])
        # The stored state did not hear it: no plan was started, and the cursor moved past it.
        state = group.state()
        self.assertEqual((state["state"], state["pending"]), (before["state"], []))
        self.assertEqual(state["revision"], before["revision"])
        self.assertFalse(any(action["kind"] == "runAgent" for action in state["pending"]))
        self.assertNotIn(f"comment-{asked['id']}", state["recentEvents"])
        self.assertEqual(group.cursor()[1], f"comment-{asked['id']}")

    def test_a_fix_command_on_a_closed_nexkit_pull_request_is_refused_without_the_engine(self):
        gh = FakeGitHub()
        cfg = make_config(autopilot={"enabled": True})
        gh.add_issue(5)
        gh.add_pull(6, 5)
        self.assertTrue(host.store_import(gh, cfg, 5))
        gh.pulls[6]["state"] = gh.issues[6]["state"] = "closed"
        before = item_state.read(gh, 5)[1]
        self.assertEqual(before["change"]["id"], "6")
        asked = gh.human_comment(6, "/nexkit fix", at="2026-01-02T00:00:01Z")
        event = {"action": "created", "issue": {"number": 6, "pull_request": {}}, "comment": asked}
        decision, sent = self.routed(gh, cfg, event)
        self.assertEqual(
            (decision["action"], decision["reason"], decision["reply_to"]),
            ("none", "The pull request is closed", 6),
        )
        # No `step` call for the command, so nothing of it is stored; the cursor passed it.
        self.assertEqual([one for one in sent if one["kind"] == "requested"], [])
        comment, state = item_state.read(gh, 5)
        self.assertEqual((state["state"], state["pending"]), (before["state"], before["pending"]))
        self.assertEqual(item_state.since(comment)[1], f"comment-{asked['id']}")

    def stored_close(self, cfg=None):
        """An issue 5 whose stored state is `accepted` with its `close` pending: the engine asked
        for it, and GitHub shows the issue closed."""
        cfg = cfg or make_config(close_parent_issues=True)
        gh = FakeGitHub()
        gh.add_issue(5, title="Part")
        self.assertTrue(host.store_import(gh, cfg, 5))
        found = item_state.read(gh, 5)
        close = {"id": "a9.1", "kind": "close", "next": None}
        pending = {**close, "outcome": "completed", "finding": None}
        doc = {**found[1], "state": "accepted", "pending": [pending], "revision": 2}
        self.assertTrue(item_state.write(gh, 5, doc, found))
        return gh, cfg

    def test_the_parents_task_of_a_closed_issue_stores_the_result_of_its_pending_close(self):
        gh, cfg = self.stored_close()
        decision = {"action": "maintain", "task": "parents", "issue": 5}
        # While the issue is open, its `close` has not happened.
        maintain(gh, decision, cfg, workflow="nexkit.yml", ref="main")
        doc = item_state.read(gh, 5)[1]
        self.assertEqual([action["kind"] for action in doc["pending"]], ["close"])
        gh.close_issue(5, "completed")
        maintain(gh, decision, cfg, workflow="nexkit.yml", ref="main")
        doc = item_state.read(gh, 5)[1]
        self.assertEqual((doc["state"], doc["pending"], doc["revision"]), ("accepted", [], 3))
        self.assertIn("closed-5-a9.1", doc["recentEvents"])
        # A second run finds nothing to report and stores nothing.
        maintain(gh, decision, cfg, workflow="nexkit.yml", ref="main")
        self.assertEqual(item_state.read(gh, 5)[1]["revision"], 3)

    def test_close_done_stores_nothing_while_the_issue_is_open_or_when_no_close_waits(self):
        gh, cfg = self.stored_close()
        self.assertFalse(host.close_done(gh, cfg, 5))
        self.assertEqual(item_state.read(gh, 5)[1]["revision"], 2)
        gh.close_issue(5, "completed")
        self.assertTrue(host.close_done(gh, cfg, 5))
        self.assertFalse(host.close_done(gh, cfg, 5))
        # An item with no state comment stores nothing.
        gh.add_issue(7, title="Other", state="closed")
        self.assertFalse(host.close_done(gh, cfg, 7))
        self.assertEqual(gh.comments(7), [])


class DispatchedFixRoundsTest(unittest.TestCase):
    """NexKit's own dispatch of the automatic fix round carries out the stored pending action
    in each round, and is never sent to the engine again as a request."""

    HEAD = "c" * 40
    RED = [{"name": "test", "passed": False, "output": "AssertionError"}]
    REVIEW = ("done", {"verdict": "approve", "criteria": [], "findings": []})

    def read(self):
        return item_state.read(self.gh, 5)

    def fail_the_checks(self):
        """The round's checks fail, which stores an automatic fix round, as `report` does."""
        found = {"action": "fix", "issue": 5, "pr": 6, "target": 6, "head": self.HEAD}
        args = (self.gh, self.cfg, found, 6, self.HEAD, 0, self.RED, None, self.REVIEW)
        end, _ = host.round_ended(*args, write=True)
        self.assertEqual(end, "auto_fix")
        return host.pending_id(self.read()[1], "runAgent", "fix")

    def publish_the_fix(self, run):
        """The fix round of the workflow run `run` ends: its agent's and publish results reach
        the state, as `report` stores them, and the next checks follow."""
        found = {"action": "fix", "issue": 5, "pr": 6, "target": 6, "head": self.HEAD}
        output = {"status": "done", "summary": "Fixed.", "checks_run": []}
        agent = {"status": "done", "output": output}
        args = (self.gh, self.cfg, found, 6, self.HEAD, agent, None, False)
        with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": str(run)}):
            self.assertTrue(host.round_published(*args))

    def dispatch(self, run):
        inputs = {"command": "fix", "number": "6", "note": "", "auto": "true"}
        event = {"inputs": inputs, "sender": {"login": BOT_LOGIN}}
        with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": str(run)}):
            return route(self.gh, "workflow_dispatch", event, inputs, cfg=self.cfg)

    def test_the_same_automatic_fix_round_starts_in_two_rounds_from_the_stored_state(self):
        self.gh = FakeGitHub()
        self.gh.add_issue(5)
        self.gh.add_pull(6, 5, head_sha=self.HEAD)
        self.cfg = make_config(fix={"max_auto_rounds": 3})
        self.assertTrue(host.store_import(self.gh, self.cfg, 5, now=IMPORTED_AT))
        ids = []
        for run in (1001, 1002):
            ids.append(self.fail_the_checks())
            before = self.read()[1]
            events = []
            real = host.step

            def spy(doc, event, found_policies, real=real, events=events):
                events.append(event["kind"])
                return real(doc, event, found_policies)

            with mock.patch.object(host, "step", spy):
                found = self.dispatch(run)
            self.assertEqual(found["action"], "fix")
            self.assertNotIn("requested", events)
            self.assertEqual(self.read()[1], before)
            self.assertEqual(before["pending"][0]["id"], ids[-1])
            self.assertFalse(any(e.startswith("dispatch-") for e in before["recentEvents"]))
            self.publish_the_fix(run)
        # Each round has its own pending action, which its dispatch carried out.
        self.assertEqual(len(set(ids)), 2, ids)
        self.assertEqual(self.read()[1]["rounds"]["counted"], 2)


class ConflictRoundsStoredTest(unittest.TestCase):
    """A conflict round that the push run starts is counted in the item's stored state by the
    run of that round, so `conflicts.max_rounds` stops the third push."""

    HEAD = "c" * 40
    GREEN = [{"name": "test", "passed": True}]
    REVIEW = ("done", {"verdict": "approve", "criteria": [], "findings": []})
    EVENT = {"ref": "refs/heads/main", "repository": {"default_branch": "main"}}

    def setUp(self):
        self.gh = FakeGitHub()
        self.gh.add_issue(5)
        self.gh.add_pull(6, 5, head_sha=self.HEAD)
        self.gh.round_comment(6, 1, self.HEAD, "passed")
        self.cfg = make_config(conflicts={"auto_resolve": True, "max_rounds": 2})
        self.assertTrue(host.store_import(self.gh, self.cfg, 5, now=IMPORTED_AT))
        self.head = self.HEAD

    def read(self):
        return item_state.read(self.gh, 5)[1]

    def push(self, sha):
        """A push to the base branch that leaves the pull request conflicting: the dispatches
        the push run made, and the lines it logged."""
        self.gh.branches["main"] = sha
        self.gh.pulls[6]["mergeable"] = False
        before = len(self.gh.dispatches)
        with mock.patch("nexkit.maintain.time.sleep"):
            lines = maintain(
                self.gh,
                route(self.gh, "push", self.EVENT),
                self.cfg,
                workflow="nexkit.yml",
                ref="main",
            )
        return [d for d in self.gh.dispatches[before:] if d["inputs"]["auto"] == "conflicts"], lines

    def run_round(self, dispatched, run):
        """The run of the dispatched round, in the item's group."""
        event = {"inputs": dispatched["inputs"], "sender": {"login": BOT_LOGIN}}
        with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": str(run)}):
            return route(self.gh, "workflow_dispatch", event, dispatched["inputs"], cfg=self.cfg)

    def end_round(self, number, sha):
        """The round merged the base branch into a new head, and its checks and review ended
        as `report` stores them."""
        head = f"{number:x}" * 40
        found = {"action": "fix", "issue": 5, "pr": 6, "target": 6, "head": self.head}
        output = {"status": "done", "summary": "Merged.", "checks_run": []}
        agent = {"status": "done", "output": output}
        args = (self.gh, self.cfg, found, 6, head, agent, None, False)
        with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": str(3000 + number)}):
            self.assertTrue(host.round_published(*args))
            args = (self.gh, self.cfg, found, 6, head, 0, self.GREEN, None, self.REVIEW)
            self.assertEqual(host.round_ended(*args, write=True, published=True)[0], "ready")
        self.gh.round_comment(6, number + 1, head, "failed", conflicts=True, base_sha=sha)
        self.gh.pulls[6]["head"]["sha"] = self.head = head

    def test_two_pushes_start_two_counted_rounds_and_the_third_starts_none(self):
        for number in (1, 2):
            sha = f"{number}" * 40
            [dispatched], _ = self.push(sha)
            self.assertEqual(self.read()["rounds"]["conflicts"], number - 1)
            self.assertEqual(self.run_round(dispatched, 2000 + number)["action"], "fix")
            self.assertEqual(self.read()["rounds"]["conflicts"], number)
            self.end_round(number, sha)
        dispatched, lines = self.push("3" * 40)
        self.assertEqual(dispatched, [])
        self.assertTrue(any("automatic conflict rounds are used up" in line for line in lines))
        self.assertEqual(self.read()["rounds"]["conflicts"], 2)

    def test_a_resumed_conflict_round_is_not_counted_again(self):
        [dispatched], _ = self.push("1" * 40)
        inputs = {**dispatched["inputs"], "auto": "conflicts,resumed"}
        self.run_round({"inputs": inputs}, 2001)
        self.assertEqual(self.read()["rounds"]["conflicts"], 0)

    def test_a_round_that_a_second_starter_finds_tried_is_not_counted(self):
        [dispatched], _ = self.push("1" * 40)
        self.gh.round_comment(6, 2, self.HEAD, "failed", conflicts=True, base_sha="1" * 40)
        self.assertEqual(self.run_round(dispatched, 2001)["action"], "none")
        self.assertEqual(self.read()["rounds"]["conflicts"], 0)


class UpdatesStoredTest(unittest.TestCase):
    """The push run stops the checks of a counted automatic fix round on a branch that is behind
    its base, updates the branch and gives the count back, and stores nothing. The review it
    dispatches stores the update and the give-back in the item's state."""

    HEAD = "c" * 40
    TIP = "f" * 40
    NEW = "e" * 40  # the head that `FakeGitHub.update_branch` leaves
    RED = [{"name": "test", "passed": False, "output": "AssertionError"}]
    REVIEW = ("done", {"verdict": "approve", "criteria": [], "findings": []})
    EVENT = {"ref": "refs/heads/main", "repository": {"default_branch": "main"}}

    def setUp(self):
        self.gh = FakeGitHub()
        self.gh.add_issue(5)
        self.gh.add_pull(6, 5, head_sha=self.HEAD)
        self.cfg = make_config(
            fix={"max_auto_rounds": 3}, conflicts={"auto_resolve": True, "max_rounds": 2}
        )
        self.assertTrue(host.store_import(self.gh, self.cfg, 5, now=IMPORTED_AT))
        # The round's checks fail: an automatic fix round is stored and counts, as `report` does.
        found = {"action": "fix", "issue": 5, "pr": 6, "target": 6, "head": self.HEAD}
        args = (self.gh, self.cfg, found, 6, self.HEAD, 0, self.RED, None, self.REVIEW)
        self.assertEqual(host.round_ended(*args, write=True)[0], "auto_fix")
        # Its checks run: the round whose checks the push run stops.
        comment, doc = item_state.read(self.gh, 5)
        result = {**host.AGENT, "changeVersion": self.HEAD}
        result["output"] = {"status": "done", "summary": "Fixed.", "checks_run": []}
        event = host._event(
            "actionFinished",
            5,
            None,
            f"run-{doc['revision']}",
            action=host.pending_id(doc, "runAgent", "fix"),
            changeVersion=self.HEAD,
            result=result,
        )
        found = host.step(doc, event, host.policies(self.cfg))
        stored = {**found["state"], "revision": doc["revision"] + 1}
        self.assertTrue(item_state.write(self.gh, 5, stored, (comment, doc)))
        # The fix is published on the same head, and its checks follow.
        comment, doc = item_state.read(self.gh, 5)
        done = {"change": {"id": "6", "version": self.HEAD}, "refused": None}
        done["observed"] = {
            "changeId": "6",
            "version": self.HEAD,
            "conflicts": False,
            "merged": False,
            "empty": False,
        }
        event = host._event(
            "actionFinished",
            5,
            None,
            f"publish-{doc['revision']}",
            action=host.pending_id(doc, "publish"),
            changeVersion=None,
            result=done,
        )
        found = host.step(doc, event, host.policies(self.cfg))
        stored = {**found["state"], "revision": doc["revision"] + 1}
        self.assertTrue(item_state.write(self.gh, 5, stored, (comment, doc)))
        self.assertEqual([a["kind"] for a in stored["pending"]], ["runChecks"])
        self.assertEqual(stored["rounds"]["counted"], 1)
        self.assertTrue(stored["rounds"]["automatic"])
        self.gh.round_comment(6, 1, self.HEAD, "failed", outcome="auto_fix")
        row = {
            "round": 2,
            "trigger": "fix (auto)",
            "action": "fix",
            "kinds": ["auto"],
            "status": "running",
            "url": "https://github.com/acme/app/actions/runs/50",
            "head": self.HEAD,
        }
        self.gh.comment(6, render_run(row))
        order = ["route", "agent", "publish", "shards", "verify", "combine"]
        jobs = [{"name": f"nexkit / {job}", "status": "completed"} for job in order[:4]]
        jobs.append({"name": "nexkit / verify (e2e)", "status": "in_progress"})
        self.gh.jobs[50] = jobs
        self.gh.runs[50] = {"status": "in_progress", "run_attempt": 1}
        self.gh.branches["main"] = self.TIP

    def read(self):
        return item_state.read(self.gh, 5)

    def push(self, conflicting=False):
        """The push run: the dispatches it made, with the state comment it left."""
        if conflicting:
            self.gh.pulls[6]["mergeable"] = False
        else:
            self.gh.strict = True
            self.gh.compares[(self.TIP, self.HEAD)] = {"behind_by": 1}
        comment, doc = self.read()
        before = (comment["body"], doc["revision"])
        with mock.patch("nexkit.maintain.time.sleep"):
            maintain(
                self.gh,
                route(self.gh, "push", self.EVENT),
                self.cfg,
                workflow="nexkit.yml",
                ref="main",
            )
        comment, doc = self.read()
        self.assertEqual((comment["body"], doc["revision"]), before)
        return [d["inputs"] for d in self.gh.dispatches]

    def dispatch(self, inputs, run=2001):
        event = {"inputs": inputs, "sender": {"login": BOT_LOGIN}}
        with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": str(run)}):
            return route(self.gh, "workflow_dispatch", event, inputs, cfg=self.cfg)

    def test_the_review_after_the_push_run_s_update_stores_it_and_gives_the_count_back(self):
        [inputs] = self.push()
        self.assertEqual(inputs["auto"], "true,carry")
        self.assertEqual(self.gh.updated, [(6, self.HEAD)])
        before = self.read()[1]
        self.assertEqual(before["rounds"]["counted"], 1)
        self.assertEqual(before["rounds"].get("updates", 0), 0)
        self.assertEqual(self.dispatch(inputs)["action"], "review")
        after = self.read()[1]
        self.assertEqual(after["rounds"]["updates"], 1)
        self.assertEqual(after["rounds"]["counted"], 0)
        self.assertEqual(after["change"]["version"], self.NEW)
        # The old checks are cancelled, and the new head's checks are pending.
        pending = {a["kind"]: a for a in after["pending"]}
        self.assertEqual(sorted(pending), ["cancel", "runChecks"])
        self.assertEqual(pending["runChecks"]["changeVersion"], self.NEW)

    def test_the_conflict_round_after_the_push_run_s_stop_gives_the_count_back(self):
        [inputs] = self.push(conflicting=True)
        self.assertEqual(inputs["auto"], "conflicts")
        self.assertEqual(self.read()[1]["rounds"]["counted"], 1)
        self.assertEqual(self.dispatch(inputs)["action"], "fix")
        rounds = self.read()[1]["rounds"]
        self.assertEqual((rounds["conflicts"], rounds["counted"]), (1, 0))

    def test_a_resumed_review_after_an_update_is_not_counted(self):
        [inputs] = self.push()
        found = self.dispatch({**inputs, "auto": "true,carry,resumed"})
        self.assertEqual(found["action"], "review")
        self.assertEqual(self.read()[1]["rounds"].get("updates", 0), 0)

    def test_the_same_dispatch_run_again_stores_the_update_once(self):
        [inputs] = self.push()
        self.assertEqual(self.dispatch(inputs)["action"], "review")
        revision = self.read()[1]["revision"]
        self.assertEqual(self.dispatch(inputs)["action"], "review")
        after = self.read()[1]
        self.assertEqual((after["revision"], after["rounds"]["updates"]), (revision, 1))


if __name__ == "__main__":
    unittest.main()
