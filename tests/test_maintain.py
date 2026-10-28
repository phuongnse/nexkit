import unittest
from unittest import mock

from nexkit.github import GitHubError
from nexkit.maintain import (
    ON_BASE_FAILED,
    UNDECIDED,
    close_parents,
    close_parents_safely,
    enabled,
    holding_commit,
    maintain,
)
from nexkit.state import render_run
from tests.support import FakeGitHub, make_config


class CloseParentTests(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        self.gh.add_issue(1, title="Epic")
        for number in (2, 3):
            self.gh.add_issue(number, title=f"Part {number}")
            self.gh.parents[number] = 1

    def close(self, number, reason="completed"):
        self.gh.issues[number].update(state="closed", state_reason=reason)

    def test_closes_the_parent_when_the_last_sub_issue_closes(self):
        self.close(2)
        self.assertEqual(close_parents(self.gh, 2), [])
        self.assertEqual(self.gh.issues[1]["state"], "open")
        self.close(3, "not_planned")
        lines = close_parents(self.gh, 3)
        self.assertEqual(lines, ["Closed #1 as completed: all its sub-issues are closed."])
        self.assertEqual(
            (self.gh.issues[1]["state"], self.gh.issues[1]["state_reason"]), ("closed", "completed")
        )
        [note] = self.gh.comments(1)
        self.assertIn("- #2: completed\n- #3: not planned", note["body"])

    def test_a_parent_whose_sub_issues_were_all_not_planned_stays_open(self):
        self.close(2, "not_planned")
        self.close(3, "duplicate")
        lines = close_parents(self.gh, 3)
        self.assertEqual(lines, ["Left #1 open: none of its sub-issues was completed."])
        self.assertEqual(self.gh.issues[1]["state"], "open")
        [note] = self.gh.comments(1)
        self.assertIn(UNDECIDED, note["body"])
        self.assertIn("A person should decide", note["body"])
        close_parents(self.gh, 3)  # the same event again adds no second note
        self.assertEqual(len(self.gh.comments(1)), 1)

    def test_goes_up_the_tree(self):
        self.gh.add_issue(10, title="Roadmap")
        self.gh.add_issue(9, title="Done before", state="closed", reason="completed")
        self.gh.parents.update({1: 10, 9: 10})
        self.close(2)
        self.close(3)
        lines = close_parents(self.gh, 3)
        self.assertEqual(len(lines), 2)
        self.assertEqual(self.gh.issues[10]["state"], "closed")

    def test_untouched_cases(self):
        self.close(2)
        self.close(3)
        self.gh.issues[1].update(state="closed", state_reason="not_planned")
        self.assertEqual(close_parents(self.gh, 3), [])
        self.assertEqual(self.gh.issues[1]["state_reason"], "not_planned")
        self.assertFalse(self.gh.comments(1))
        self.gh.add_issue(7)
        self.assertEqual(close_parents(self.gh, 7), [])  # no parent

    def test_a_parent_in_another_repository_is_left_alone(self):
        self.close(2)
        self.close(3)
        self.gh.issues[1]["repository_url"] = "https://api.github.com/repos/acme/other"
        self.assertEqual(close_parents(self.gh, 3), [])
        self.assertEqual(self.gh.issues[1]["state"], "open")
        self.gh.issues[1]["repository_url"] = "https://api.github.com/repos/ACME/app"
        self.assertEqual(len(close_parents(self.gh, 3)), 1)

    def test_sub_issues_closed_together_close_the_parent_once(self):
        self.close(2)
        self.close(3)
        close_parents(self.gh, 2)
        self.gh.issues[1]["state"] = "open"  # the second run read it before the first closed it
        close_parents(self.gh, 3)
        self.assertEqual(len(self.gh.comments(1)), 1)

    def test_errors_become_a_line(self):
        self.close(2)
        self.close(3)
        self.gh.close_errors[1] = GitHubError(403, "Forbidden")
        [line] = close_parents_safely(self.gh, 3)
        self.assertIn("Could not check the parent issue of #3: GitHub API 403", line)

    def test_maintain_runs_the_task(self):
        self.close(2)
        self.close(3)
        decision = {"action": "maintain", "task": "parents", "issue": 3}
        maintain(self.gh, decision, make_config(close_parent_issues=True))
        self.assertEqual(self.gh.issues[1]["state"], "closed")


if __name__ == "__main__":
    unittest.main()


class OnBaseTests(unittest.TestCase):
    """A pull request whose base branch already holds its change."""

    BROUGHT = "b" * 40

    def setUp(self):
        self.gh = FakeGitHub()
        self.gh.add_issue(5)
        self.gh.add_pull(6, 5, head_sha="e" * 40)
        self.gh.on_base.add("e" * 40)
        row = {"round": 1, "trigger": "review (auto)", "url": "https://x/6/1", "status": "success"}
        self.gh.comment(6, render_run({**row, "outcome": "branch_updated"}))
        self.decision = {
            "action": "maintain",
            "task": "on_base",
            "issue": 5,
            "pr": 6,
            "base": "main",
            "head": "e" * 40,
            "merge_base": "c" * 40,
        }

    def history(self):
        """The pull request started at `0…`; its own commit changed one file, which `main`
        changed in a commit it merged since, and in one before it started."""
        start, own = "0" * 40, "1" * 40
        self.gh.own_commits[6] = [
            {"sha": own, "parents": [{"sha": start}]},
            {"sha": "2" * 40, "parents": [{"sha": own}, {"sha": "c" * 40}]},
        ]
        self.gh.commit_files[own] = ["scripts/compose.test.mjs"]
        commits = [{"sha": "a" * 40}, {"sha": self.BROUGHT}, {"sha": "c" * 40}]
        self.gh.compares[(start, "c" * 40)] = {"commits": commits}
        self.gh.path_history["scripts/compose.test.mjs"] = [self.BROUGHT, "9" * 40]

    def maintain(self, cfg=None):
        return maintain(self.gh, self.decision, cfg or make_config(), workflow="n.yml", ref="main")

    def test_names_the_commit_that_brought_the_change(self):
        self.history()
        self.assertEqual(holding_commit(self.gh, 6, "c" * 40), self.BROUGHT)
        # Two commits that changed its files since it started: NexKit names neither.
        self.gh.path_history["scripts/compose.test.mjs"].append("a" * 40)
        self.assertIsNone(holding_commit(self.gh, 6, "c" * 40))
        self.gh.pull_commits = mock.Mock(side_effect=GitHubError(500, "Server Error"))
        self.assertIsNone(holding_commit(self.gh, 6, "c" * 40))

    def test_closes_the_pull_request_and_its_issue_with_the_commit(self):
        self.history()
        self.assertEqual(self.maintain(), ["#6: already_on_base."])
        self.assertEqual(self.gh.pulls[6]["state"], "closed")
        self.assertEqual(
            (self.gh.issues[5]["state"], self.gh.issues[5]["state_reason"]), ("closed", "completed")
        )
        [note] = self.gh.comments(5)
        self.assertEqual(
            note["body"],
            f"NexKit closed #6 without merging: `main` already holds its change, brought by "
            f"{self.BROUGHT}.",
        )
        [row] = self.gh.run_comments(6)
        self.assertEqual(
            (row["run"]["status"], row["run"]["outcome"], row["run"]["on_base"]),
            ("success", "already_on_base", self.BROUGHT),
        )
        self.assertIn(
            f"`main` already holds the change of this pull request, brought by {self.BROUGHT}, "
            "so it changes no file against `main`. NexKit closed it without merging.\n\n"
            "Closed #5 as completed.",
            row["body"],
        )

    def test_without_a_commit_it_says_so_without_one(self):
        cfg = make_config(autopilot={"enabled": True})
        self.assertEqual(
            self.maintain(cfg), ["#6: already_on_base.", "Started an autopilot sweep."]
        )
        [note] = self.gh.comments(5)
        self.assertEqual(
            note["body"], "NexKit closed #6 without merging: `main` already holds its change."
        )
        self.assertNotIn("on_base", self.gh.run_comments(6)[0]["run"])

    def test_an_issue_it_cannot_close_waits_for_a_person(self):
        self.gh.close_errors[5] = GitHubError(403, "Resource not accessible by integration")
        self.assertEqual(self.maintain(), ["#6: on_base_failed."])
        self.assertEqual(self.gh.pulls[6]["state"], "closed")
        [row] = self.gh.run_comments(6)
        self.assertEqual(
            (row["run"]["status"], row["run"]["attention"]), ("failure", ON_BASE_FAILED)
        )
        self.assertIn("Could not close #5: GitHub API 403", row["body"])

    def test_runs_with_every_setting_off(self):
        self.assertTrue(enabled("on_base", make_config()))
