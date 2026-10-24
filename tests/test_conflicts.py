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
from nexkit.github import GitHubError, run_url
from nexkit.maintain import is_conflicting, maintain, resolve_conflicts
from nexkit.route import route
from nexkit.state import BOT_LOGIN, render_run
from tests.support import FakeGitHub, GitRepos, git, make_config
from tests.test_report import HEAD, ReportCase, decision
from tests.test_route import comment_event

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
        self.assertEqual(self.sweep(), [])
        self.assertFalse(self.gh.dispatches)
        # A run that ended without a report holds nothing up.
        self.gh.runs[40] = {"status": "completed"}
        self.assertEqual(self.sweep(), ["Started an automatic round on #6 to merge `main`."])

    def test_unknown_mergeability(self):
        self.gh.pulls[6]["mergeable"] = None
        self.assertIsNone(is_conflicting(self.gh, 6, tries=2, delay=0))
        self.assertEqual(self.sweep(), ["GitHub has not worked out yet whether #6 conflicts."])

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
        cfg = make_config(merge={"auto": True}, conflicts={"auto_resolve": True})
        self.assertEqual(self.run_report(cfg=cfg)["outcome"], "conflict_fix")
        [dispatch] = self.gh.dispatches
        self.assertEqual(dispatch["inputs"]["auto"], "conflicts")
        [row] = self.gh.run_comments(6)
        self.assertIn("It conflicts with `main`. Started an automatic round on #6", row["body"])

    def refused_while_running(self, *runs, max_rounds=3):
        """A merge refused for a conflict, reported by a round whose own comment still says
        running."""
        os.environ["GITHUB_RUN_ID"] = "1"  # the id the next report runs as
        for run in runs:
            self.gh.comment(6, render_run(run))
        own = {"round": len(runs) + 1, "trigger": "review", "url": run_url(), "status": "running"}
        self.gh.comment(6, render_run(own))
        self.candidate()
        self.gh.merge_error = GitHubError(405, "Pull Request has merge conflicts")
        self.gh.pulls[6]["mergeable"] = False
        conflicts = {"auto_resolve": True, "max_rounds": max_rounds}
        cfg = make_config(merge={"auto": True}, conflicts=conflicts)
        return self.run_report(cfg=cfg)["outcome"]

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
        [dispatch] = self.gh.dispatches
        self.assertEqual(dispatch["inputs"]["auto"], "conflicts")

    def test_a_refused_merge_tries_each_base_commit_once(self):
        self.assertEqual(self.refused_while_running(self.tried(BASE_SHA)), "merge_refused")
        self.assertFalse(self.gh.dispatches)

    def test_a_refused_merge_keeps_the_round_limit(self):
        outcome = self.refused_while_running(self.tried("e" * 40), max_rounds=1)
        self.assertEqual(outcome, "merge_refused")
        self.assertFalse(self.gh.dispatches)
        self.assertIn("conflict rounds are used up", self.gh.run_comments(6)[-1]["body"])

    def test_a_merge_refused_for_another_reason_waits_for_a_person(self):
        self.candidate()
        self.gh.merge_error = GitHubError(405, "Required approving review")
        cfg = make_config(merge={"auto": True}, conflicts={"auto_resolve": True})
        self.assertNotEqual(self.run_report(cfg=cfg)["outcome"], "conflict_fix")
        self.assertFalse(self.gh.dispatches)

    def test_a_merge_checks_the_other_pull_requests(self):
        self.gh.add_issue(4)
        self.gh.add_pull(7, 4)
        self.gh.pulls[7]["mergeable"] = False
        self.candidate()
        self.run_report(cfg=make_config(merge={"auto": True}))
        self.assertFalse(self.gh.dispatches)  # off by default
        cfg = make_config(merge={"auto": True}, conflicts={"auto_resolve": True})
        self.run_report(cfg=cfg)
        self.assertEqual([d["inputs"]["number"] for d in self.gh.dispatches], ["7"])
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
        self.assertFalse(self.gh.dispatches)


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
        [dispatch] = self.gh.dispatches
        self.assertEqual(
            (dispatch["inputs"]["number"], dispatch["inputs"]["auto"]), ("6", "conflicts")
        )
        self.assertIn(
            "It conflicts with `main`. Started an automatic round on #6", outcome["summary"]
        )
        # Without a conflict, nothing more starts.
        self.gh.dispatches.clear()
        self.gh.pulls[6]["mergeable"] = True
        self.run_report(cfg=cfg)
        self.assertFalse(self.gh.dispatches)

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

    def test_a_merge_waiting_whose_checks_fail_waits_for_a_person(self):
        from nexkit import autopilot

        self.gh.add_milestone(1)
        self.gh.issues[5]["milestone"] = {"number": 1}
        self.candidate()
        self.gh.merge_error = [GitHubError(405, 'Required status check "e2e" is in progress.')]
        self.run_report(cfg=self.cfg)
        self.gh.merge_error = GitHubError(405, 'Required status check "e2e" is failing.')
        autopilot.sweep(self.gh, self.cfg, "nexkit.yml", "main")
        row = self.gh.run_comments(6)[-1]["run"]
        self.assertEqual(row["outcome"], "merge_refused")
        self.assertTrue(self.gh.comments_matching(6, "@olivia a person is needed"))

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
