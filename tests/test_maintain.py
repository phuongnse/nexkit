import unittest

from nexkit.github import GitHubError
from nexkit.maintain import UNDECIDED, close_parents, close_parents_safely, maintain
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
