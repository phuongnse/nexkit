"""Automatic rounds that merge the base branch into NexKit pull requests."""

import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from nexkit import agent, cli
from nexkit.github import GitHubError, run_url
from nexkit.maintain import is_conflicting, maintain, resolve_conflicts
from nexkit.route import route
from nexkit.state import BOT_LOGIN, read_state, render_run, render_state
from nexkit.usage import iso
from tests.support import CALC, SUB, FakeGitHub, GitRepos, git, make_config
from tests.test_report import HEAD, ReportCase, decision
from tests.test_route import comment_event

BASE_SHA = "f" * 40


def rounds_started(gh):
    """The rounds dispatched, without the sweep that the end of a run starts."""
    return [d for d in gh.dispatches if d["inputs"]["command"] != "autopilot"]


class SweepTests(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        for number, issue in ((6, 5), (7, 4), (8, 3)):
            self.gh.add_issue(issue)
            self.gh.add_pull(number, issue)
        self.gh.pulls[6]["mergeable"] = False
        self.gh.add_pull(9, 2, branch="feature/x")
        self.gh.pulls[9]["mergeable"] = False

    def sweep(self, **kw):
        return resolve_conflicts(self.gh, "main", "nexkit.yml", "main", 3, delay=0, **kw)

    def round(self, number, n, **run):
        row = {"round": n, "trigger": "fix", "url": f"https://x/{number}/{n}", "status": "success"}
        self.gh.comment(number, render_run({**row, **run}))

    def test_starts_a_round_only_for_conflicting_nexkit_pull_requests(self):
        self.assertEqual(self.sweep(), ["Started an automatic round on #6 to merge `main`."])
        [dispatch] = self.gh.dispatches
        self.assertEqual(dispatch["inputs"]["auto"], "conflicts")
        self.assertEqual(dispatch["inputs"]["number"], "6")
        self.assertIn("Merge `main` into this branch", dispatch["inputs"]["note"])
        self.assertEqual(self.sweep(skip={6}), [])

    def test_the_same_base_commit_is_tried_once(self):
        self.round(6, 1, conflicts=True, base_sha=BASE_SHA, status="failure")
        self.assertEqual(self.sweep(), [])
        self.gh.branches["main"] = "e" * 40
        self.assertEqual(len(self.sweep()), 1)

    def test_rounds_are_limited_per_pull_request(self):
        for n in range(1, 4):
            self.round(6, n, conflicts=True, base_sha=str(n) * 40)
        self.round(6, 9, conflicts=True, resumed=True, base_sha="9" * 40)  # not counted twice
        [line] = self.sweep()
        self.assertIn("automatic conflict rounds are used up", line)
        self.assertFalse(self.gh.dispatches)

    def test_a_round_held_up_by_workflow_changes_is_not_counted(self):
        for n in range(1, 3):
            self.round(6, n, conflicts=True, base_sha=str(n) * 40)
        self.round(6, 3, conflicts=True, base_sha="3" * 40, outcome="needs_merge")
        # While it is the latest round, a person merges: each push to `main` would only
        # stop the same way.
        self.assertEqual(self.sweep(), ["#6 conflicts with `main`, which a person merges."])
        self.assertFalse(self.gh.dispatches)
        # Once a person merged and a round ran, conflict rounds start again, and the round
        # held up does not count.
        self.round(6, 4, trigger="review")
        self.assertEqual(self.sweep(), ["Started an automatic round on #6 to merge `main`."])

    def test_the_round_limit_is_configured(self):
        self.round(6, 1, conflicts=True, base_sha="1" * 40)
        [line] = resolve_conflicts(self.gh, "main", "nexkit.yml", "main", 1, delay=0)
        self.assertIn("its 1 automatic conflict rounds are used up", line)

    def test_a_paused_or_running_round_is_not_replaced(self):
        self.round(6, 1, status="paused")
        self.assertEqual(self.sweep(), [])
        self.round(6, 2, status="running")
        self.assertEqual(self.sweep(), [])
        self.round(6, 3, status="continued")  # its command runs again
        self.assertEqual(self.sweep(), [])
        self.assertFalse(self.gh.dispatches)

    def test_a_pull_request_whose_implement_round_runs_gets_no_conflict_round(self):
        go = {"command": "go", "url": "https://github.com/acme/app/actions/runs/40"}
        self.gh.comment(5, render_run({**go, "status": "running"}))
        self.gh.runs[40] = {"status": "in_progress"}
        self.assertEqual(self.sweep(), [])
        self.assertFalse(self.gh.dispatches)
        # A run that ended without a report, or whose run cannot be read, holds nothing up.
        self.gh.runs[40] = {"status": "completed"}
        self.assertEqual(self.sweep(), ["Started an automatic round on #6 to merge `main`."])

    def test_unknown_mergeability(self):
        self.gh.pulls[6]["mergeable"] = None
        self.assertIsNone(is_conflicting(self.gh, 6, tries=2, delay=0))
        self.assertEqual(self.sweep(), ["GitHub has not worked out yet whether #6 conflicts."])

    def swept(self, **cfg):
        """One sweep with `conflicts.auto_resolve` and autopilot off. Returns the rounds it
        started."""
        cfg = make_config(conflicts={"auto_resolve": True}, **cfg)
        before = len(self.gh.dispatches)
        with mock.patch("nexkit.maintain.time.sleep"):
            maintain(self.gh, {"task": "sweep"}, cfg, workflow="nexkit.yml", ref="main")
        return [d["inputs"]["number"] for d in self.gh.dispatches[before:]]

    def test_a_sweep_starts_the_round_of_a_conflicting_pull_request_once(self):
        # `main` moved while a fix round ran, which held the conflict round
        # back; the round then stopped on an error. Nothing started it until a person did.
        self.round(6, 1, status="running")
        self.assertEqual(self.swept(), [])
        self.round(6, 2, status="failure", outcome="agent_error")
        self.assertEqual(self.swept(), ["6"])
        # The start is recorded on the latest round, so the next sweep, before the
        # conflict round posts its comment, does not start it twice.
        self.assertEqual(self.gh.run_comments(6)[-1]["run"]["conflicts_started"], BASE_SHA)
        self.assertEqual(self.swept(), [])
        # Its comment says it runs, and then that it is blocked on this base commit.
        self.round(6, 3, status="running", conflicts=True, base_sha=BASE_SHA)
        self.assertEqual(self.swept(), [])
        self.gh.issue_comments[6][-1]["body"] = render_run(
            {**self.gh.run_comments(6)[-1]["run"], "status": "failure", "outcome": "agent_blocked"}
        )
        self.assertEqual(self.swept(), [])
        # The base branch moves: a new round.
        self.gh.branches["main"] = "e" * 40
        self.assertEqual(self.swept(), ["6"])

    def test_a_round_that_ended_without_a_report_holds_nothing_up_without_autopilot(self):
        # The fix round's runner was lost: its `finish` job starts a sweep, which with
        # autopilot off only starts conflict rounds. It ends the round's comment itself.
        url = "https://github.com/acme/app/actions/runs/40"
        self.round(6, 1, status="running", url=url, head="b" * 40)
        self.gh.runs[40] = {"status": "in_progress"}
        self.assertEqual(self.swept(), [])
        self.gh.runs[40] = {"status": "completed", "conclusion": "failure"}
        self.assertEqual(self.swept(), ["6"])
        run = self.gh.run_comments(6)[-1]["run"]
        self.assertEqual((run["status"], run["outcome"]), ("failure", "lost"))
        self.assertEqual(run["conflicts_started"], BASE_SHA)
        self.assertEqual({s["state"] for s in self.gh.statuses}, {"error"})
        # With autopilot, its sweep handles such a round first, and may start it once more.
        self.round(6, 2, status="running", url=url)
        with mock.patch("nexkit.autopilot.sweep", return_value=[]):
            self.assertEqual(self.swept(autopilot={"enabled": True}), [])
        self.assertEqual(self.gh.run_comments(6)[-1]["run"]["status"], "running")

    def test_a_round_that_started_the_next_one_holds_the_sweep_back(self):
        # An automatic fix round is on its way and merges the base branch itself; a
        # dispatch now would replace it in the queue.
        self.round(6, 1, outcome="auto_fix")
        self.gh.issue_comments[6][-1]["updated_at"] = iso(datetime.now(UTC))
        self.assertEqual(self.swept(), [])
        # One that did not run within hours holds nothing up.
        self.gh.issue_comments[6][-1]["updated_at"] = "2020-01-01T00:00:00Z"
        self.assertEqual(self.swept(), ["6"])

    def test_a_round_after_the_tried_one_lets_the_same_base_commit_be_tried_again(self):
        self.round(6, 1, conflicts=True, base_sha=BASE_SHA, status="failure")
        self.assertEqual(self.swept(), [])
        self.round(6, 2, trigger="review", status="success", outcome="ready")
        self.assertEqual(self.swept(), ["6"])

    def test_unknown_mergeability_waits_for_the_next_sweep(self):
        self.gh.pulls[6]["mergeable"] = None
        self.assertEqual(self.swept(), [])
        self.gh.pulls[6]["mergeable"] = False
        self.assertEqual(self.swept(), ["6"])

    def test_a_sweep_leaves_alone_a_pull_request_autopilot_started_a_command_on(self):
        busy = {6}
        with mock.patch("nexkit.autopilot.sweep", side_effect=lambda *a: a[-1].update(busy) or []):
            self.assertEqual(self.swept(autopilot={"enabled": True}), [])
        with mock.patch("nexkit.autopilot.sweep", return_value=["Autopilot: nothing."]):
            self.assertEqual(self.swept(autopilot={"enabled": True}), ["6"])

    def test_maintain_tasks(self):
        cfg = make_config(conflicts={"auto_resolve": True})
        with mock.patch("nexkit.maintain.time.sleep"):
            maintain(
                self.gh, {"task": "conflicts", "base": "main"}, cfg, workflow="n.yml", ref="main"
            )
            self.assertEqual(len(self.gh.dispatches), 1)
            self.gh.branches["main"] = "e" * 40
            maintain(self.gh, {"task": "schedule"}, cfg, workflow="n.yml", ref="main")
            self.assertEqual(len(self.gh.dispatches), 2)
            maintain(self.gh, {"task": "schedule"}, make_config(), workflow="n.yml", ref="main")
            self.assertEqual(len(self.gh.dispatches), 2)


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        self.gh.add_issue(5)
        self.gh.add_pull(6, 5)

    def test_pushes_to_the_default_branch(self):
        event = {"ref": "refs/heads/main", "repository": {"default_branch": "main"}}
        self.assertEqual(
            route(self.gh, "push", event),
            {"action": "maintain", "task": "conflicts", "base": "main"},
        )
        event["ref"] = "refs/heads/feature"
        self.assertEqual(route(self.gh, "push", event)["action"], "none")

    def test_a_persons_command_waits_while_the_implement_round_runs(self):
        go = {"command": "go", "url": "https://github.com/acme/app/actions/runs/40"}
        self.gh.comment(5, render_run({**go, "status": "running"}))
        self.gh.runs[40] = {"status": "in_progress"}
        for event_name, event in (
            ("issue_comment", comment_event(6, "/nexkit fix Tidy it", on_pull=True)),
            (
                "pull_request_review",
                {
                    "action": "submitted",
                    "pull_request": {"number": 6},
                    "review": {"state": "changes_requested", "user": {"login": "alice"}},
                },
            ),
        ):
            decision = route(self.gh, event_name, event)
            self.assertEqual((decision["action"], decision["reply_to"]), ("none", 6))
            self.assertIn(
                "[implement round](https://github.com/acme/app/actions/runs/40)", decision["reason"]
            )
            self.assertIn("one NexKit round at a time", decision["reason"])
        # The rounds that the implement round starts itself as it ends go on.
        inputs = {"command": "review", "number": "6", "note": "", "auto": "retry"}
        dispatch = route(self.gh, "workflow_dispatch", {"sender": {"login": BOT_LOGIN}}, inputs)
        self.assertEqual(dispatch["action"], "review")
        # Once it ended, the command runs.
        self.gh.comment(5, render_run({**go, "status": "success"}))
        fix = route(self.gh, "issue_comment", comment_event(6, "/nexkit fix", on_pull=True))
        self.assertEqual(fix["action"], "fix")

    def test_a_conflict_round_records_the_base_commit(self):
        self.gh.branches["main"] = "e" * 40
        inputs = {"command": "fix", "number": "6", "note": "Merge", "auto": "conflicts"}
        decision = route(self.gh, "workflow_dispatch", {"sender": {"login": BOT_LOGIN}}, inputs)
        self.assertEqual(decision["base_sha"], "e" * 40)
        self.assertTrue(decision["conflicts"])


class ConcurrentStartTests(unittest.TestCase):
    """A push to the default branch and a sweep look at a conflicting pull request at the
    same moment, before either records its start: both start a conflict round."""

    def setUp(self):
        self.gh = FakeGitHub()
        self.gh.add_issue(5)
        self.gh.add_pull(6, 5)
        self.gh.pulls[6]["mergeable"] = False
        row = {"round": 1, "trigger": "fix", "url": "https://x/6/1", "status": "success"}
        self.gh.comment(6, render_run(row))

    def test_the_second_conflict_round_started_at_the_same_time_runs_nothing(self):
        read, seen = self.gh.comments, {}

        def stale(number):
            # Each starter reads the comments before the other one writes its start.
            return copy.deepcopy(seen.setdefault(number, copy.deepcopy(read(number))))

        with mock.patch.object(self.gh, "comments", side_effect=stale):
            for _ in range(2):
                resolve_conflicts(self.gh, "main", "nexkit.yml", "main", 3, delay=0)
        started = [d["inputs"] for d in rounds_started(self.gh)]
        self.assertEqual(len(started), 2)
        # The first runs and ends on this base commit.
        sender = {"sender": {"login": BOT_LOGIN}}
        first = route(self.gh, "workflow_dispatch", sender, started[0])
        self.assertEqual(first["action"], "fix")
        row = {"round": 2, "url": "https://x/6/2", "status": "failure", "outcome": "agent_blocked"}
        row.update(trigger="fix (auto, conflicts)", conflicts=True, base_sha=first["base_sha"])
        self.gh.comment(6, render_run(row))
        # The second, which waited behind it in the pull request's group, runs nothing.
        second = route(self.gh, "workflow_dispatch", sender, started[1])
        self.assertEqual((second["action"], second["reply_to"]), ("none", None))
        self.assertIn("A conflict round already ran on `main`", second["reason"])
        # A round that resumes or retries it, or starts once more after it ended without a
        # report, goes on.
        for auto in ("conflicts,resume", "conflicts,retry"):
            again = route(self.gh, "workflow_dispatch", sender, {**started[1], "auto": auto})
            self.assertEqual(again["action"], "fix", auto)
        self.gh.issue_comments[6][-1]["body"] = render_run({**row, "outcome": "lost"})
        self.assertEqual(route(self.gh, "workflow_dispatch", sender, started[1])["action"], "fix")
        # After the base branch moves, a new round goes on too.
        self.gh.issue_comments[6][-1]["body"] = render_run(row)
        self.gh.branches["main"] = "e" * 40
        self.assertEqual(route(self.gh, "workflow_dispatch", sender, started[1])["action"], "fix")


MUL_TOP = "def mul(a, b):\n    return a * b\n\n\n"
DIV = "\n\ndef div(a, b):\n    return a / b\n"
DIV_TOP = "def div(a, b):\n    return a / b\n\n\n"


class UpToDateTests(unittest.TestCase):
    def setUp(self):
        self.repos = GitRepos()
        self.repo = self.repos.clone("work")
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()
        self.repos.cleanup()

    def test_conflicts_with_base(self):
        self.repos.diverge()
        git(self.repo, "fetch", "-q", "origin")
        git(self.repo, "checkout", "-q", "-B", "nexkit/issue-5", "origin/nexkit/issue-5")
        self.assertTrue(agent.conflicts_with_base(self.repo, "main"))
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "reset", "-q", "--hard", "origin/main")
        self.assertFalse(agent.conflicts_with_base(self.repo, "main"))

    def checkout(self, branch="nexkit/issue-5"):
        git(self.repo, "fetch", "-q", "origin")
        git(self.repo, "checkout", "-q", "-B", branch, f"origin/{branch}")

    def run_agent(self, **fields):
        (self.out / "context.json").write_text("{}")
        d = decision("fix", pr=6, target=6, head=HEAD, auto=True, **fields)
        env = {"NEXKIT_DECISION": json.dumps(d), "NEXKIT_CONFIG": json.dumps(make_config())}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
            cli.main(["agent", "--repo", str(self.repo), "--out", str(self.out)])
        return json.loads((self.out / "result.json").read_text())

    def test_a_conflict_round_without_a_conflict_runs_nothing(self):
        self.repos.push_commit("dev", "nexkit/issue-5", {"calc.py": CALC + SUB}, "Add sub")
        self.checkout()
        self.assertEqual(self.run_agent(conflicts=True)["status"], "up_to_date")

    def test_a_change_the_base_branch_already_holds_runs_nothing(self):
        self.repos.push_commit("dev", "nexkit/issue-5", {"calc.py": CALC + SUB}, "Add sub")
        self.repos.push_commit("dev", "main", {"README.md": "Calc\n"}, "Readme")
        # Another pull request makes the same change while it adds `mul` to the same file.
        brought = self.repos.push_commit("dev", "main", {"calc.py": MUL_TOP + CALC + SUB}, "Both")
        base = self.repos.push_commit(
            "dev", "main", {"calc.py": DIV_TOP + MUL_TOP + CALC + SUB}, "Add div"
        )
        self.checkout()
        result = self.run_agent()
        self.assertEqual(
            {key: result[key] for key in ("status", "merge_base", "on_base")},
            {"status": "already_on_base", "merge_base": base, "on_base": brought},
        )
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")  # nothing merged

    def test_a_change_of_its_own_goes_on(self):
        self.repos.push_commit("dev", "nexkit/issue-5", {"calc.py": CALC + SUB + DIV}, "Sub, div")
        both = self.repos.push_commit("dev", "main", {"calc.py": MUL_TOP + CALC + SUB}, "Both")
        self.checkout()
        self.assertIsNone(agent.on_base(self.repo, "main"))
        # Once its own commits drop what `main` lacks, `main` holds all of it.
        self.repos.push_commit("dev", "nexkit/issue-5", {"calc.py": CALC + SUB}, "Drop div")
        self.checkout()
        self.assertEqual(agent.on_base(self.repo, "main")["on_base"], both)


class ConflictReportTests(ReportCase):
    def test_a_round_whose_change_is_already_on_base_closes_and_gives_its_round_back(self):
        state = {"version": 2, "issue": 5, "auto_fixes": 1, "feedback": None}
        self.gh.comment(6, render_state(state))
        found = {
            "stage": "fix",
            "status": "already_on_base",
            "summary": "",
            "error": "`main` already holds the change of this pull request.",
            "merge_base": "c" * 40,
            "on_base": "b" * 40,
        }
        self.write("nexkit-agent", "result.json", found)
        outcome = self.run_report(d=decision("fix", pr=6, target=6, head=HEAD, auto=True))
        self.assertEqual(outcome["outcome"], "already_on_base")
        self.assertEqual(self.gh.pulls[6]["state"], "closed")
        self.assertEqual(self.gh.issues[5]["state_reason"], "completed")
        self.assertFalse(self.gh.merged)
        [row] = self.gh.run_comments(6)
        self.assertEqual(
            (row["run"]["status"], row["run"]["outcome"], row["run"]["on_base"]),
            ("success", "already_on_base", "b" * 40),
        )
        self.assertIn(
            f"already holds the change of this pull request, brought by {'b' * 40}", row["body"]
        )
        self.assertEqual(read_state(self.gh.comments(6))[1]["auto_fixes"], 0)

    def test_an_up_to_date_round_succeeds_without_changes(self):
        self.write("nexkit-agent", "result.json", agent.up_to_date("main"))
        d = decision("fix", pr=6, target=6, head=HEAD, auto=True, conflicts=True)
        outcome = self.run_report(d=d, published=False)
        self.assertEqual(outcome["outcome"], "up_to_date")
        [row] = self.gh.run_comments(6)
        self.assertEqual(row["run"]["status"], "success")
        self.assertEqual(row["run"]["trigger"], "fix (auto, conflicts)")
        self.assertIn("merges cleanly into this pull request now", row["body"])

    def test_a_merge_refused_for_a_conflict_starts_a_conflict_round(self):
        self.candidate()
        self.gh.merge_error = GitHubError(405, "Pull Request is not mergeable")
        self.gh.pulls[6]["mergeable"] = False
        cfg = make_config(merge={"auto": True}, conflicts={"auto_resolve": True})
        self.assertEqual(self.run_report(cfg=cfg)["outcome"], "conflict_fix")
        [dispatch] = rounds_started(self.gh)
        self.assertEqual(dispatch["inputs"]["auto"], "conflicts")
        [row] = self.gh.run_comments(6)
        self.assertIn("It conflicts with `main`. Started an automatic round on #6", row["body"])

    def refused_while_running(self, *runs, max_rounds=3, d=None):
        """A merge refused for a conflict, reported by a round whose own comment still says
        running."""
        os.environ["GITHUB_RUN_ID"] = "1"  # the id the next report runs as
        for run in runs:
            self.gh.comment(6, render_run(run))
        own = {"round": len(runs) + 1, "trigger": "review", "url": run_url(), "status": "running"}
        if d and d.get("conflicts"):
            own.update(trigger="fix (auto, conflicts)", conflicts=True, base_sha=d["base_sha"])
        self.gh.comment(6, render_run(own))
        self.candidate()
        self.gh.merge_error = GitHubError(405, "Pull Request has merge conflicts")
        self.gh.pulls[6]["mergeable"] = False
        conflicts = {"auto_resolve": True, "max_rounds": max_rounds}
        cfg = make_config(merge={"auto": True}, conflicts=conflicts)
        return self.run_report(cfg=cfg, **({"d": d} if d else {}))["outcome"]

    def tried(self, sha):
        return {
            "round": 1,
            "trigger": "fix (auto, conflicts)",
            "url": "https://x/1",
            "status": "failure",
            "conflicts": True,
            "base_sha": sha,
        }

    def test_the_reporting_round_does_not_block_its_own_conflict_round(self):
        self.assertEqual(self.refused_while_running(), "conflict_fix")
        [dispatch] = rounds_started(self.gh)
        self.assertEqual(dispatch["inputs"]["auto"], "conflicts")

    def test_a_refused_merge_tries_each_base_commit_once(self):
        # The conflict round itself reports: the base branch did not move since it started.
        d = decision("fix", pr=6, target=6, head=HEAD, auto=True, conflicts=True)
        d["base_sha"] = BASE_SHA
        self.assertEqual(self.refused_while_running(d=d), "merge_refused")
        self.assertFalse(rounds_started(self.gh))

    def test_a_refused_merge_tries_again_after_another_round_ended(self):
        # A review round ran after the conflict round on the same base commit.
        self.assertEqual(self.refused_while_running(self.tried(BASE_SHA)), "conflict_fix")
        [row] = [r for r in self.gh.run_comments(6) if r["run"]["url"] == run_url()]
        self.assertEqual(row["run"]["conflicts_started"], BASE_SHA)

    def test_a_refused_merge_keeps_the_round_limit(self):
        outcome = self.refused_while_running(self.tried("e" * 40), max_rounds=1)
        self.assertEqual(outcome, "merge_refused")
        self.assertFalse(rounds_started(self.gh))
        self.assertIn("conflict rounds are used up", self.gh.run_comments(6)[-1]["body"])

    def test_the_end_of_any_round_starts_the_conflict_round_held_back_while_it_ran(self):
        # `main` moved while a fix round ran, and the round stopped on
        # an error without trying a merge. Its end sweeps, which starts the conflict round,
        # also without autopilot.
        self.gh.pulls[6]["mergeable"] = False
        self.write("nexkit-agent", "result.json", {"status": "error", "error": "No change."})
        d = decision("fix", pr=6, target=6, head=HEAD, auto=True)
        cfg = make_config(conflicts={"auto_resolve": True})
        self.assertEqual(self.run_report(d=d, cfg=cfg, published=False)["outcome"], "agent_error")
        [sweep] = self.gh.dispatches
        with mock.patch("nexkit.maintain.time.sleep"):
            maintain(
                self.gh,
                route(self.gh, "workflow_dispatch", {}, sweep["inputs"]),
                cfg,
                workflow="nexkit.yml",
                ref="main",
            )
        [started] = rounds_started(self.gh)
        self.assertEqual(
            (started["inputs"]["number"], started["inputs"]["auto"]), ("6", "conflicts")
        )

    def test_the_end_of_a_run_sweeps_for_conflicts_unless_a_round_is_on_its_way(self):
        from nexkit import autopilot

        cfg = make_config(conflicts={"auto_resolve": True})

        def sweeps(outcome):
            self.gh.dispatches.clear()
            autopilot.after_run(self.gh, cfg, {"issue": 5}, outcome, "nexkit.yml", "main")
            return len(self.gh.dispatches)

        self.assertEqual([sweeps(o) for o in ("ready", "agent_error", "needs_human")], [1, 1, 1])
        self.assertEqual([sweeps(o) for o in ("auto_fix", "conflict_fix", "paused")], [0, 0, 0])
        # A run whose report did not finish sweeps too, after it ended.
        self.gh.dispatches.clear()
        autopilot.after_unreported(self.gh, cfg, "nexkit.yml", "main", 40)
        self.assertEqual(self.gh.dispatches[0]["inputs"]["note"], "after run 40")
        self.gh.dispatches.clear()
        autopilot.after_run(self.gh, make_config(), {"issue": 5}, "ready", "nexkit.yml", "main")
        self.assertFalse(self.gh.dispatches)

    def test_a_merge_refused_for_another_reason_waits_for_a_person(self):
        self.candidate()
        self.gh.merge_error = GitHubError(405, "Required approving review")
        cfg = make_config(merge={"auto": True}, conflicts={"auto_resolve": True})
        self.assertNotEqual(self.run_report(cfg=cfg)["outcome"], "conflict_fix")
        self.assertFalse(rounds_started(self.gh))

    def test_a_merge_checks_the_other_pull_requests(self):
        self.gh.add_issue(4)
        self.gh.add_pull(7, 4)
        self.gh.pulls[7]["mergeable"] = False
        self.candidate()
        self.run_report(cfg=make_config(merge={"auto": True}))
        self.assertFalse(rounds_started(self.gh))  # off by default
        cfg = make_config(merge={"auto": True}, conflicts={"auto_resolve": True})
        self.run_report(cfg=cfg)
        self.assertEqual([d["inputs"]["number"] for d in rounds_started(self.gh)], ["7"])
        row = self.gh.run_comments(6)[-1]
        self.assertIn("Started an automatic round on #7 to merge `main`.", row["body"])

    def test_a_merge_skips_another_pull_request_whose_round_is_running(self):
        self.gh.add_issue(4)
        self.gh.add_pull(7, 4)
        self.gh.pulls[7]["mergeable"] = False
        self.gh.comment(
            7,
            render_run({"round": 1, "trigger": "fix", "url": "https://x/7/1", "status": "running"}),
        )
        self.candidate()
        cfg = make_config(merge={"auto": True}, conflicts={"auto_resolve": True})
        self.assertEqual(self.run_report(cfg=cfg)["outcome"], "merged")
        self.assertFalse(rounds_started(self.gh))


if __name__ == "__main__":
    unittest.main()


class MergeRefusalTests(ReportCase):
    """Refusals of the automatic merge that pass by themselves or by NexKit's own rounds."""

    def setUp(self):
        super().setUp()
        sleep = mock.patch("nexkit.report.time.sleep")
        self.sleeps = sleep.start()
        self.addCleanup(sleep.stop)
        self.cfg = make_config(
            merge={"auto": True},
            conflicts={"auto_resolve": True, "max_rounds": 1},
            autopilot={"enabled": True},
            notify=["olivia"],
        )

    def test_a_merge_refused_because_the_base_branch_moved_is_tried_again(self):
        self.candidate()
        modified = GitHubError(405, "Base branch was modified. Review and try the merge again.")
        self.gh.merge_error = [modified, modified]
        outcome = self.run_report(cfg=self.cfg)
        self.assertEqual(outcome["outcome"], "merged")
        self.assertIn("at try 3", outcome["summary"])
        self.assertEqual(self.gh.issues[5]["state"], "closed")
        # Three refusals in a row: a person decides.
        self.gh.issues[5]["state"] = "open"
        self.gh.merge_error = [modified] * 3
        self.assertEqual(self.run_report(cfg=self.cfg)["outcome"], "merge_refused")

    def test_a_merge_refused_because_the_head_moved_reviews_the_new_head(self):
        self.candidate()
        self.gh.merge_error = GitHubError(
            409, "Head branch was modified. Review and try the merge again."
        )
        self.gh.pulls[6]["head"]["sha"] = "c" * 40
        outcome = self.run_report(cfg=self.cfg)
        self.assertEqual(outcome["outcome"], "head_moved")
        self.assertIsNone(outcome.get("attention"))
        self.assertIn(
            "The branch moved to `ccccccc` after this round checked it", outcome["summary"]
        )
        # The review is the next round; no sweep starts, and nobody is mentioned.
        [dispatch] = self.gh.dispatches
        self.assertEqual(
            dispatch["inputs"], {"command": "review", "number": "6", "note": "", "auto": "true"}
        )
        self.assertFalse(self.gh.comments_matching(5, "@olivia"))
        self.assertFalse(self.gh.comments_matching(6, "@olivia"))

    def test_an_implement_round_that_ends_with_a_conflict_starts_its_conflict_round(self):
        os.environ["GITHUB_RUN_ID"] = "1"  # the id the next report runs as
        self.gh.comment(5, render_run({"command": "go", "url": run_url(), "status": "running"}))
        self.candidate()
        self.gh.pulls[6]["mergeable"] = False
        cfg = make_config(conflicts={"auto_resolve": True})
        outcome = self.run_report(cfg=cfg)
        self.assertEqual(outcome["outcome"], "ready")
        [dispatch] = rounds_started(self.gh)
        self.assertEqual(
            (dispatch["inputs"]["number"], dispatch["inputs"]["auto"]), ("6", "conflicts")
        )
        self.assertIn(
            "It conflicts with `main`. Started an automatic round on #6", outcome["summary"]
        )
        # Its round comment, posted as it reports, records the start, so the sweep at its
        # end does not start the round twice.
        self.assertEqual(self.gh.run_comments(6)[-1]["run"]["conflicts_started"], "f" * 40)
        [sweep] = [d for d in self.gh.dispatches if d["inputs"]["command"] == "autopilot"]
        with mock.patch("nexkit.maintain.time.sleep"):
            maintain(
                self.gh,
                route(self.gh, "workflow_dispatch", {}, sweep["inputs"]),
                cfg,
                workflow="nexkit.yml",
                ref="main",
            )
        self.assertEqual(len(rounds_started(self.gh)), 1)
        # Without a conflict, nothing more starts.
        self.gh.dispatches.clear()
        self.gh.pulls[6]["mergeable"] = True
        self.run_report(cfg=cfg)
        self.assertFalse(rounds_started(self.gh))

    def test_a_merge_waiting_for_required_checks_is_merged_by_a_later_sweep(self):
        from nexkit import autopilot

        self.gh.add_milestone(1)
        self.gh.issues[5]["milestone"] = {"number": 1}
        self.candidate()
        pending = GitHubError(405, 'Required status check "e2e" is expected.')
        self.gh.merge_error = [pending]
        outcome = self.run_report(cfg=self.cfg)
        self.assertEqual(outcome["outcome"], "merge_waiting")
        self.assertIsNone(outcome.get("attention"))
        self.assertIn("The next autopilot sweep merges it", outcome["summary"])
        # The checks pass: the next sweep merges and closes the issue.
        self.gh.merge_error = None
        lines = autopilot.sweep(self.gh, self.cfg, "nexkit.yml", "main")
        self.assertIn("#6: merged.", lines)
        self.assertEqual(self.gh.issues[5]["state"], "closed")
        row = self.gh.run_comments(6)[-1]
        self.assertEqual((row["run"]["outcome"], row["run"]["status"]), ("merged", "success"))

    def test_a_merge_waiting_for_checks_without_a_sweep_to_merge_it_waits_for_a_person(self):
        self.gh.add_milestone(1)
        pending = GitHubError(405, 'Required status check "e2e" is expected.')
        # Without autopilot, or outside its scope, a person merges it.
        manual = make_config(merge={"auto": True})
        for cfg in (manual, make_config(merge={"auto": True}, autopilot={"enabled": True})):
            self.gh.merge_error = [pending]
            self.candidate()
            if cfg is not manual:
                self.gh.issues[5]["milestone"] = None
            outcome = self.run_report(cfg=cfg)
            self.assertEqual(outcome["outcome"], "merge_refused")
            self.assertIn("Merge it once they pass.", outcome["summary"])
            self.assertTrue(outcome["attention"])

    def test_a_failed_merge_step_says_so_and_the_sweep_goes_on(self):
        from nexkit import autopilot

        self.gh.add_milestone(1)
        self.gh.issues[5]["milestone"] = {"number": 1}
        self.candidate()
        self.gh.merge_error = [GitHubError(405, 'Required status check "e2e" is expected.')]
        self.run_report(cfg=self.cfg)
        with mock.patch("nexkit.report.merge_pull", side_effect=GitHubError(502, "Bad")):
            lines = autopilot.sweep(self.gh, self.cfg, "nexkit.yml", "main")
        self.assertIn("Could not merge the pull request of #6: GitHub API 502: Bad", lines)

    def waiting_merge(self):
        """An approved round of #6 whose merge waits for the required check `e2e`."""
        self.gh.add_milestone(1)
        self.gh.issues[5]["milestone"] = {"number": 1}
        self.candidate()
        self.gh.merge_error = [GitHubError(405, 'Required status check "e2e" is in progress.')]
        self.assertEqual(self.run_report(cfg=self.cfg)["outcome"], "merge_waiting")
        self.gh.dispatches.clear()

    def e2e_fails(self):
        """`e2e`, a job of the repository's CI, fails on the approved head in `make e2e`."""
        self.gh.commit_checks[HEAD] = [
            {"id": 77, "name": "e2e", "conclusion": "failure", "html_url": "https://job/77"},
            {"id": 78, "name": "lint", "conclusion": "success", "html_url": "https://job/78"},
        ]
        self.gh.logs[77] = b"##[group]Run make e2e\n##[error]Process completed with exit code 1.\n"
        self.gh.annotations[77] = [
            {"annotation_level": "failure", "title": "Cart > empties", "path": "e2e/cart.spec.ts"}
        ]
        self.gh.merge_error = GitHubError(405, 'Required status check "e2e" is failing.')

    def test_a_merge_waiting_whose_required_check_fails_gets_an_automatic_fix_round(self):
        from nexkit import autopilot
        from nexkit.context import _after_failed_checks, _feedback
        from nexkit.state import read_state

        self.waiting_merge()
        self.e2e_fails()
        lines = autopilot.sweep(self.gh, self.cfg, "nexkit.yml", "main")
        self.assertIn("#6: auto_fix.", lines)
        row = self.gh.run_comments(6)[-1]
        self.assertEqual((row["run"]["outcome"], row["run"]["status"]), ("auto_fix", "success"))
        self.assertIn(
            "Required check `e2e` failed on `ccccccc` while the merge waited for it. "
            "Starting automatic fix round 1 of 2.",
            row["body"],
        )
        [fix] = rounds_started(self.gh)
        self.assertEqual(
            fix["inputs"], {"command": "fix", "number": "6", "note": "", "auto": "true"}
        )
        # The round counts toward `fix.max_auto_rounds`, and its feedback names the check.
        state = read_state(self.gh.comments(6))[1]
        self.assertEqual(state["auto_fixes"], 1)
        required = {
            "name": "e2e",
            "url": "https://job/77",
            "commands": ["make e2e"],
            "tests": ["cart.spec.ts::empties"],
        }
        self.assertEqual(state["feedback"]["required"], [required])
        decision = {"pr": 6, "head": HEAD, "auto": True}
        text = _feedback(self.gh, decision, self.gh.comments(6))
        self.assertIn("### Required check `e2e` failed, so GitHub refused the merge", text)
        self.assertIn("Commands that failed in it: `make e2e`.", text)
        # Like any automatic round after failing checks, it merges a base that moved first.
        self.assertTrue(_after_failed_checks(decision, self.gh.comments(6)))
        self.assertFalse(self.gh.comments_matching(6, "@olivia"))
        self.assertFalse(self.gh.comments_matching(5, "@olivia"))

    def test_a_required_check_that_failed_before_the_first_merge_gets_a_fix_round(self):
        # CI failed before NexKit's review ended: the round's own merge is refused.
        from nexkit import progress
        from nexkit.context import _after_failed_checks
        from nexkit.state import read_state

        self.candidate()
        self.e2e_fails()
        self.gh.merge_error = [self.gh.merge_error]
        outcome = self.run_report(cfg=self.cfg)
        self.assertEqual((outcome["outcome"], outcome["attention"]), ("auto_fix", None))
        self.assertIn(
            "Required check `e2e` failed on `ccccccc`, so GitHub refused the merge. "
            "Starting automatic fix round 1 of 2.",
            outcome["summary"],
        )
        # One fix round, counted once, with the failed check in its feedback.
        [fix] = rounds_started(self.gh)
        self.assertEqual(
            fix["inputs"], {"command": "fix", "number": "6", "note": "", "auto": "true"}
        )
        state = read_state(self.gh.comments(6))[1]
        self.assertEqual(state["auto_fixes"], 1)
        self.assertEqual([item["name"] for item in state["feedback"]["required"]], ["e2e"])
        decision = {"pr": 6, "head": HEAD, "auto": True}
        self.assertTrue(_after_failed_checks(decision, self.gh.comments(6)))
        self.assertFalse(self.gh.comments_matching(6, "@olivia"))
        # With no automatic round left, a person decides.
        self.gh.dispatches.clear()
        comment_id, state = read_state(self.gh.comments(6))
        state["auto_fixes"] = 2
        progress.save_state(self.gh, 6, comment_id, state)
        self.gh.merge_error = [GitHubError(405, 'Required status check "e2e" is failing.')]
        self.assertEqual(self.run_report(cfg=self.cfg)["outcome"], "needs_human")
        self.assertFalse(rounds_started(self.gh))

    def test_a_merge_waiting_whose_required_check_fails_without_rounds_left_waits(self):
        from nexkit import autopilot, progress
        from nexkit.state import read_state

        self.waiting_merge()
        comment_id, state = read_state(self.gh.comments(6))
        state["auto_fixes"] = 2
        progress.save_state(self.gh, 6, comment_id, state)
        self.e2e_fails()
        autopilot.sweep(self.gh, self.cfg, "nexkit.yml", "main")
        row = self.gh.run_comments(6)[-1]["run"]
        self.assertEqual(row["outcome"], "needs_human")
        self.assertIn("no automatic fix rounds remain", row["attention"])
        self.assertFalse(rounds_started(self.gh))
        self.assertEqual(read_state(self.gh.comments(6))[1]["auto_fixes"], 2)
        self.assertTrue(self.gh.comments_matching(6, "@olivia a person is needed"))

    def test_a_merge_waiting_whose_checks_never_pass_waits_for_a_person_after_hours(self):
        from datetime import timedelta

        from nexkit import autopilot

        self.waiting_merge()
        later = datetime.now(UTC) + timedelta(hours=7)
        [step] = [s for s in autopilot.preview(self.gh, self.cfg)[1] if s["number"] == 5]
        self.assertEqual(step["state"], "merge")
        step = autopilot.assess(self.gh, self.cfg, self.gh.issues[5], {5: 6}, now=later)
        self.assertEqual((step["state"], step["person"]), ("waits", True))
        self.assertIn("have not passed for hours", step["reason"])

    def test_a_new_commit_on_a_merge_waiting_pull_request_is_reviewed(self):
        from nexkit import autopilot

        self.waiting_merge()
        self.gh.pulls[6]["head"]["sha"] = "e" * 40
        lines = autopilot.sweep(self.gh, self.cfg, "nexkit.yml", "main")
        self.assertIn(
            "Started /nexkit review on #6: pull request #6 got the new commit `eeeeeee` while "
            "its merge waited for required checks.",
            lines,
        )
        [review] = rounds_started(self.gh)
        self.assertEqual(
            review["inputs"], {"command": "review", "number": "6", "note": "", "auto": "true"}
        )
        self.assertFalse(self.gh.merged)
        # It starts once: the next sweep waits for it, and nobody is mentioned.
        autopilot.sweep(self.gh, self.cfg, "nexkit.yml", "main")
        self.assertEqual(len(rounds_started(self.gh)), 1)
        self.assertFalse(self.gh.comments_matching(5, "@olivia"))
        self.assertFalse(self.gh.comments_matching(6, "@olivia"))

    def test_a_branch_behind_its_base_is_updated_and_reviewed_again(self):
        self.candidate()
        self.gh.merge_error = GitHubError(405, "Head branch is out of date. Review and try again.")
        outcome = self.run_report(cfg=self.cfg)
        self.assertEqual(outcome["outcome"], "branch_updated")
        self.assertEqual(self.gh.updated, [(6, HEAD)])
        [dispatch] = self.gh.dispatches
        self.assertEqual(dispatch["inputs"]["command"], "review")
        self.assertTrue(self.gh.run_comments(6)[-1]["run"]["updated_branch"])
        # Once per `conflicts.max_rounds`: a person decides after that.
        self.gh.pulls[6]["head"]["sha"] = HEAD
        self.assertEqual(self.run_report(cfg=self.cfg)["outcome"], "merge_refused")
        self.assertEqual(len(self.gh.updated), 1)

    def test_a_refusal_that_needs_a_person_names_githubs_reason(self):
        self.candidate()
        self.gh.merge_error = GitHubError(405, "At least 1 approving review is required.")
        outcome = self.run_report(cfg=self.cfg)
        self.assertEqual(outcome["outcome"], "merge_refused")
        self.assertIn("At least 1 approving review is required", outcome["summary"])
        self.assertFalse(self.gh.dispatches[:-1])  # only the autopilot sweep after the run
        self.assertEqual(self.gh.updated, [])
