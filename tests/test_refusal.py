"""What a refused merge's mention says: the rule, the step that unblocks it, and for a code
owner review the files and their owners."""

import unittest
from unittest import mock

from nexkit import autopilot, refusal
from nexkit.github import GitHubError
from nexkit.maintain import maintain
from nexkit.refusal import codeowners, explain, owners_of
from nexkit.state import render_run
from tests.support import FakeGitHub, make_config
from tests.test_report import HEAD, ReportCase

# What GitHub answers when a ruleset refuses a merge for a code owner review.
OWNER_REFUSAL = GitHubError(
    405,
    '{"message":"Repository rule violations found\\n\\nWaiting on code owner review from '
    'olivia.\\n\\n","documentation_url":"https://docs.github.com/rest"}',
)
TARGET = {"pr": 6, "base": "main"}


class CodeOwnersTests(unittest.TestCase):
    def owners(self, text, path):
        return owners_of(codeowners(text), path)

    def test_the_last_matching_pattern_names_the_owners(self):
        text = "# Owners\n* @olivia\n*.js @js-team # scripts\n/docs/ @writer @acme/docs\n"
        self.assertEqual(self.owners(text, "app.py"), ["@olivia"])
        self.assertEqual(self.owners(text, "web/app.js"), ["@js-team"])
        self.assertEqual(self.owners(text, "docs/guide/a.md"), ["@writer", "@acme/docs"])
        # Anchored at the root: another folder named docs is not it.
        self.assertEqual(self.owners(text, "web/docs/a.md"), ["@olivia"])

    def test_patterns_follow_githubs_rules(self):
        cases = [
            ("apps/ @a", "src/apps/x.py", ["@a"]),  # a folder anywhere
            ("/build/logs/ @a", "build/logs/x/y.log", ["@a"]),
            ("/build/logs/ @a", "src/build/logs/y.log", []),
            ("docs/* @a", "docs/getting-started.md", ["@a"]),
            ("docs/* @a", "docs/build-app/troubleshooting.md", []),  # not deeper
            ("**/logs @a", "deeply/nested/logs/x.log", ["@a"]),
            ("src/**/test.py @a", "src/a/b/test.py", ["@a"]),
            ("README.md @a", "sub/README.md", ["@a"]),
            ("*.md @a\n/docs/x.md", "docs/x.md", []),  # a pattern without owners
            ("file?.txt @a", "file1.txt", ["@a"]),
        ]
        for text, path, expected in cases:
            with self.subTest(text=text, path=path):
                self.assertEqual(self.owners(text, path), expected)


class ExplainTests(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        self.gh.add_pull(6, 5)

    def test_a_code_owner_review_names_the_files_their_owners_and_the_lines(self):
        self.gh.rules = [
            {"type": "pull_request", "parameters": {"require_code_owner_review": True}}
        ]
        self.gh.review_states[6] = {"decision": "REVIEW_REQUIRED"}
        self.gh.files[".github/CODEOWNERS"] = "/docs/reference/ @olivia\n*.py @alice\n"
        self.gh.pr_files[6] = [
            {"filename": "docs/reference/ui-glossary.json", "additions": 6, "deletions": 0},
            {"filename": "app.py", "additions": 2, "deletions": 1},
            {"filename": "README.md", "additions": 1, "deletions": 1},
        ]
        # Alice approved, so her file needs nothing more.
        self.gh.pr_reviews[6] = [{"user": {"login": "alice"}, "state": "APPROVED"}]
        do, why = explain(self.gh, TARGET, OWNER_REFUSAL)
        self.assertEqual(
            do, "approve the pull request as a code owner (`@olivia`), which NexKit then merges"
        )
        self.assertEqual(
            why,
            "GitHub requires a code owner's review for this file, and NexKit's App cannot "
            "review its own pull request: `docs/reference/ui-glossary.json` (+6 −0, owned by "
            "`@olivia`)",
        )

    def test_githubs_words_name_a_code_owner_review_that_classic_protection_requires(self):
        # NexKit's token may not read classic protection: no rule says it.
        self.gh.review_states[6] = {"decision": "REVIEW_REQUIRED"}
        self.gh.files["CODEOWNERS"] = "* @olivia\n"
        self.gh.pr_files[6] = [{"filename": "app.py", "additions": 1, "deletions": 0}]
        do, _ = explain(self.gh, TARGET, OWNER_REFUSAL)
        self.assertIn("as a code owner (`@olivia`)", do)
        # Without a CODEOWNERS file, NexKit still says whose review it waits for.
        del self.gh.files["CODEOWNERS"]
        do, why = explain(self.gh, TARGET, OWNER_REFUSAL)
        self.assertEqual(do, "a code owner approves the pull request, which NexKit then merges")
        self.assertIn("requires a code owner's review", why)

    def test_many_owned_files_are_counted(self):
        self.gh.review_states[6] = {"decision": "REVIEW_REQUIRED"}
        self.gh.rules = [
            {"type": "pull_request", "parameters": {"require_code_owner_review": True}}
        ]
        self.gh.files["CODEOWNERS"] = "* @olivia @acme/core\n"
        self.gh.pr_files[6] = [{"filename": f"f{n}.py"} for n in range(7)]
        _, why = explain(self.gh, TARGET, OWNER_REFUSAL)
        self.assertTrue(why.endswith("; and 2 more"), why)
        self.assertIn("owned by `@olivia`, `@acme/core`", why)

    def test_required_approvals(self):
        rule = {"required_approving_review_count": 2, "require_code_owner_review": False}
        self.gh.rules = [{"type": "pull_request", "parameters": rule}]
        self.gh.review_states[6] = {"decision": "REVIEW_REQUIRED"}
        self.gh.pr_reviews[6] = [
            {"user": {"login": "alice"}, "state": "CHANGES_REQUESTED"},
            {"user": {"login": "alice"}, "state": "APPROVED"},
            {"user": {"login": "olivia"}, "state": "COMMENTED"},
        ]
        refused = GitHubError(405, "At least 2 approving reviews are required.")
        self.assertEqual(
            explain(self.gh, TARGET, refused),
            (
                "approve the pull request, which NexKit then merges",
                "GitHub requires 2 approving reviews (1 given), and NexKit's App cannot review "
                "its own pull request",
            ),
        )

    def test_a_review_that_requests_changes(self):
        self.gh.review_states[6] = {"decision": "CHANGES_REQUESTED"}
        self.gh.pr_reviews[6] = [{"user": {"login": "olivia"}, "state": "CHANGES_REQUESTED"}]
        do, why = explain(self.gh, TARGET, GitHubError(405, "Changes requested"))
        self.assertEqual(
            do,
            "the reviewer who requested changes (`@olivia`) approves the pull request, which "
            "NexKit then merges, or dismisses the review",
        )
        self.assertIn("while a review requests changes", why)

    def test_conversations_draft_queue_signatures_deployments_and_squash(self):
        cases = [
            (
                [
                    {
                        "type": "pull_request",
                        "parameters": {"required_review_thread_resolution": True},
                    }
                ],
                {"unresolved": 2},
                {},
                "resolve the pull request's 2 open review conversations, then merge it",
            ),
            ([], {"draft": True}, {}, "mark the pull request ready for review, then merge it"),
            ([{"type": "merge_queue"}], {}, {}, "add the pull request to the merge queue"),
            (
                [{"type": "required_signatures"}],
                {},
                {},
                "merge the pull request yourself, or let NexKit's App bypass the rule",
            ),
            (
                [
                    {
                        "type": "required_deployments",
                        "parameters": {"required_deployment_environments": ["staging"]},
                    }
                ],
                {},
                {},
                "deploy the pull request to `staging`, then merge it",
            ),
            (
                [],
                {},
                {"allow_squash_merge": False},
                "allow squash merges in the repository's settings, or merge the pull request "
                "yourself",
            ),
        ]
        for rules, state, settings, do in cases:
            with self.subTest(do):
                self.gh.rules = rules
                self.gh.review_states[6] = state
                self.gh.repo_settings.update({"allow_squash_merge": True, **settings})
                self.assertEqual(explain(self.gh, TARGET, GitHubError(405, "no"))[0], do)

    def test_any_other_rule_quotes_githubs_answer(self):
        refused = GitHubError(405, '{"message": "Something new\\n@team <!-- x -->"}')
        self.assertEqual(
            explain(self.gh, TARGET, refused),
            (
                refusal.FALLBACK_DO,
                "GitHub refused the merge: `Something new @team ‹!-- x --›`",
            ),
        )

    def test_a_read_that_fails_leaves_githubs_answer(self):
        with mock.patch.object(self.gh, "reviews", side_effect=GitHubError(502, "down")):
            self.gh.review_states[6] = {"decision": "CHANGES_REQUESTED"}
            do, why = refusal.explain_safely(self.gh, TARGET, OWNER_REFUSAL)
        self.assertEqual(do, refusal.FALLBACK_DO)
        self.assertIn("Waiting on code owner review from olivia.", why)


class RefusedMergeReportTests(ReportCase):
    def test_the_round_and_the_mention_say_what_unblocks_the_merge(self):
        self.candidate()
        self.gh.rules = [
            {"type": "pull_request", "parameters": {"require_code_owner_review": True}}
        ]
        self.gh.review_states[6] = {"decision": "REVIEW_REQUIRED"}
        self.gh.files[".github/CODEOWNERS"] = "/docs/ @olivia\n"
        self.gh.pr_files[6] = [{"filename": "docs/a.md", "additions": 6, "deletions": 0}]
        self.gh.merge_error = OWNER_REFUSAL
        outcome = self.run_report(cfg=make_config(merge={"auto": True}, notify=["owner"]))
        self.assertEqual(outcome["outcome"], "merge_refused")
        reason = (
            "approve the pull request as a code owner (`@olivia`), which NexKit then merges. "
            "GitHub requires a code owner's review for this file, and NexKit's App cannot "
            "review its own pull request: `docs/a.md` (+6 −0, owned by `@olivia`)"
        )
        self.assertEqual(outcome["attention"], reason)
        [row] = self.gh.run_comments(6)
        self.assertIn(f"**What unblocks it:** {reason}.", row["body"])
        self.assertEqual(row["run"]["attention"], reason)
        mentions = [c["body"] for c in self.gh.comments(6) if c["body"].startswith("@owner")]
        self.assertEqual(mentions, [f"@owner a person is needed: {reason}."])


APPROVED = {"action": "maintain", "task": "approved", "pr": 6, "issue": 5, "actor": "olivia"}


class ApprovedMergeTests(ReportCase):
    """A writer's approval, or GitHub's `clean`, merges a pull request it refused."""

    def setUp(self):
        super().setUp()
        self.cfg = make_config(merge={"auto": True}, notify=["owner"])
        self.candidate()
        self.gh.review_states[6] = {"decision": "REVIEW_REQUIRED"}
        self.gh.files["CODEOWNERS"] = "* @olivia\n"
        self.gh.pr_files[6] = [{"filename": "app.py", "additions": 1, "deletions": 0}]
        self.gh.merge_error = OWNER_REFUSAL
        self.assertEqual(self.run_report(cfg=self.cfg)["outcome"], "merge_refused")
        self.gh.merge_error = None

    def approve(self, cfg=None):
        return maintain(self.gh, APPROVED, cfg or self.cfg, workflow="nexkit.yml", ref="main")

    def latest(self):
        return self.gh.run_comments(6)[-1]["run"]

    def test_an_approval_merges_the_pull_request_on_the_head_that_passed(self):
        self.assertEqual(self.approve(), ["#6: merged."])
        self.assertEqual(self.gh.merged, [(6, HEAD, "squash")])
        self.assertEqual(self.gh.issues[5]["state"], "closed")
        run = self.latest()
        self.assertEqual((run["outcome"], run["status"]), ("merged", "success"))
        self.assertNotIn("attention", run)

    def test_an_approval_after_the_head_moved_reviews_the_new_head(self):
        self.gh.pulls[6]["head"]["sha"] = "e" * 40
        self.gh.merge_error = GitHubError(409, "Head branch was modified.")
        self.assertEqual(self.approve(), ["#6: head_moved."])
        self.assertEqual(self.gh.merged, [])
        [review] = [d for d in self.gh.dispatches if d["inputs"]["command"] == "review"]
        self.assertEqual(review["inputs"]["number"], "6")

    def test_an_approval_merges_nothing_for_a_closed_issue_or_an_untrusted_edit(self):
        self.gh.issues[5]["state"] = "closed"
        self.assertEqual(self.approve(), ["#6: merge_issue_closed."])
        self.gh.issues[5]["state"] = "open"
        self.gh.round_comment(6, 2, HEAD, "passed", outcome="merge_refused")
        self.gh.human_comment(5, "/nexkit trust")
        self.gh.edits[5] = [("bob", "2026-02-01T00:00:00Z")]
        self.assertEqual(self.approve(), [f"#6: {autopilot.UNTRUSTED_MERGE}."])
        self.assertEqual(self.gh.merged, [])

    def test_an_approval_starts_nothing_after_another_outcome_or_on_hold(self):
        self.gh.round_comment(6, 2, HEAD, "failed", outcome="needs_human")
        self.assertEqual(
            self.approve(),
            ["#6: its latest round is `needs_human`, so the approval starts nothing."],
        )
        self.gh.round_comment(6, 3, HEAD, "passed", outcome="merge_refused")
        self.gh.issues[5]["labels"] = [{"name": "on-hold"}]
        held = make_config(
            merge={"auto": True}, autopilot={"enabled": True, "labels": {"on-hold": {"hold": True}}}
        )
        self.gh.add_milestone(1)
        self.gh.issues[5]["milestone"] = {"number": 1}
        self.assertEqual(
            self.approve(held), ["#6: #5 is on hold (`on-hold`), so NexKit does not merge it."]
        )
        self.assertEqual(self.gh.merged, [])

    def test_an_approval_confirms_criteria_the_review_could_not_verify(self):
        row = {"round": 2, "trigger": "fix", "url": "u", "status": "success", "head": HEAD}
        self.gh.comment(6, render_run({**row, "outcome": "ready"}, "Ready."))
        self.assertEqual(self.approve(), ["#6: merged."])

    def test_a_refusal_for_another_reason_stays_and_says_why(self):
        self.gh.merge_error = GitHubError(
            405, '{"message": "Commits must have verified signatures."}'
        )
        self.gh.rules = [{"type": "required_signatures"}]
        self.gh.review_states[6] = {"decision": "APPROVED"}
        self.assertEqual(self.approve(), ["#6: merge_refused."])
        self.assertIn("requires signed commits", self.latest()["attention"])
        mentions = [c["body"] for c in self.gh.comments(6) if c["body"].startswith("@owner")]
        self.assertIn("requires signed commits", mentions[-1])
        self.assertIn("@olivia approved the pull request.", self.gh.run_comments(6)[-1]["body"])


class CleanMergeSweepTests(ReportCase):
    """Each sweep merges a refused pull request that GitHub now reports as `clean`."""

    def setUp(self):
        super().setUp()
        self.gh.add_milestone(1)
        self.gh.issues[5]["milestone"] = {"number": 1}
        self.cfg = make_config(merge={"auto": True}, autopilot={"enabled": True})
        self.candidate()
        self.gh.merge_error = GitHubError(405, "At least 1 approving review is required.")
        self.assertEqual(self.run_report(cfg=self.cfg)["outcome"], "merge_refused")

    def sweep(self):
        return autopilot.sweep(self.gh, self.cfg, "nexkit.yml", "main")

    def test_a_refused_merge_merges_once_github_reports_it_clean(self):
        self.gh.pulls[6]["mergeable_state"] = "blocked"
        self.sweep()
        self.assertEqual(self.gh.merged, [])
        self.gh.pulls[6]["mergeable_state"] = "clean"
        self.gh.merge_error = None
        self.sweep()
        self.assertEqual(self.gh.merged, [(6, HEAD, "squash")])
        self.assertEqual(self.gh.issues[5]["state"], "closed")

    def test_a_clean_head_that_github_still_refuses_is_tried_once(self):
        self.gh.pulls[6]["mergeable_state"] = "clean"
        self.sweep()
        self.assertEqual(self.gh.run_comments(6)[-1]["run"]["refused_clean"], HEAD)
        tried = len(self.gh.comments(6))
        self.sweep()
        self.assertEqual(len(self.gh.comments(6)), tried)
        # A new head or a person's approval still tries it.
        self.assertEqual(
            maintain(self.gh, APPROVED, self.cfg, workflow="nexkit.yml", ref="main"),
            ["#6: merge_refused."],
        )

    def test_a_head_that_moved_is_not_merged(self):
        self.gh.pulls[6]["mergeable_state"] = "clean"
        self.gh.pulls[6]["head"]["sha"] = "e" * 40
        self.gh.merge_error = None
        self.sweep()
        self.assertEqual(self.gh.merged, [])


if __name__ == "__main__":
    unittest.main()
