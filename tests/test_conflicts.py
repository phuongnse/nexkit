"""Automatic rounds that merge the base branch into NexKit pull requests."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nexkit import agent, cli
from nexkit.github import GitHubError
from nexkit.maintain import MAX_CONFLICT_ROUNDS, is_conflicting, maintain, resolve_conflicts
from nexkit.route import route
from nexkit.state import BOT_LOGIN, render_run
from tests.support import FakeGitHub, GitRepos, git, make_config
from tests.test_report import HEAD, ReportCase, decision

BASE_SHA = "f" * 40


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
        return resolve_conflicts(self.gh, "main", "nexkit.yml", "main", delay=0, **kw)

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
        for n in range(1, MAX_CONFLICT_ROUNDS + 1):
            self.round(6, n, conflicts=True, base_sha=str(n) * 40)
        self.round(6, 9, conflicts=True, resumed=True, base_sha="9" * 40)  # not counted twice
        [line] = self.sweep()
        self.assertIn("automatic conflict rounds are used up", line)
        self.assertFalse(self.gh.dispatches)

    def test_a_paused_or_running_round_is_not_replaced(self):
        self.round(6, 1, status="paused")
        self.assertEqual(self.sweep(), [])
        self.round(6, 2, status="running")
        self.assertEqual(self.sweep(), [])
        self.assertFalse(self.gh.dispatches)

    def test_unknown_mergeability(self):
        self.gh.pulls[6]["mergeable"] = None
        self.assertIsNone(is_conflicting(self.gh, 6, tries=2, delay=0))
        self.assertEqual(self.sweep(), ["GitHub has not worked out yet whether #6 conflicts."])

    def test_maintain_tasks(self):
        cfg = make_config(auto_resolve_conflicts=True)
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

    def test_a_conflict_round_records_the_base_commit(self):
        self.gh.branches["main"] = "e" * 40
        inputs = {"command": "fix", "number": "6", "note": "Merge", "auto": "conflicts"}
        decision = route(self.gh, "workflow_dispatch", {"sender": {"login": BOT_LOGIN}}, inputs)
        self.assertEqual(decision["base_sha"], "e" * 40)
        self.assertTrue(decision["conflicts"])


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

    def test_a_conflict_round_without_a_conflict_runs_nothing(self):
        (self.out / "context.json").write_text("{}")
        d = decision("fix", pr=6, target=6, head=HEAD, auto=True, conflicts=True)
        env = {"NEXKIT_DECISION": json.dumps(d), "NEXKIT_CONFIG": json.dumps(make_config())}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
            cli.main(["agent", "--repo", str(self.repo), "--out", str(self.out)])
        result = json.loads((self.out / "result.json").read_text())
        self.assertEqual(result["status"], "up_to_date")


class ConflictReportTests(ReportCase):
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
        cfg = make_config(auto_merge=True, auto_resolve_conflicts=True)
        self.assertEqual(self.run_report(cfg=cfg)["outcome"], "conflict_fix")
        [dispatch] = self.gh.dispatches
        self.assertEqual(dispatch["inputs"]["auto"], "conflicts")
        [row] = self.gh.run_comments(6)
        self.assertIn("It conflicts with `main`. Started an automatic round on #6", row["body"])

    def test_a_merge_refused_for_another_reason_waits_for_a_person(self):
        self.candidate()
        self.gh.merge_error = GitHubError(405, "Required approving review")
        cfg = make_config(auto_merge=True, auto_resolve_conflicts=True)
        self.assertNotEqual(self.run_report(cfg=cfg)["outcome"], "conflict_fix")
        self.assertFalse(self.gh.dispatches)

    def test_a_merge_checks_the_other_pull_requests(self):
        self.gh.add_issue(4)
        self.gh.add_pull(7, 4)
        self.gh.pulls[7]["mergeable"] = False
        self.candidate()
        self.run_report(cfg=make_config(auto_merge=True))
        self.assertFalse(self.gh.dispatches)  # off by default
        cfg = make_config(auto_merge=True, auto_resolve_conflicts=True)
        self.run_report(cfg=cfg)
        self.assertEqual([d["inputs"]["number"] for d in self.gh.dispatches], ["7"])
        row = self.gh.run_comments(6)[-1]
        self.assertIn("Started an automatic round on #7 to merge `main`.", row["body"])


if __name__ == "__main__":
    unittest.main()
