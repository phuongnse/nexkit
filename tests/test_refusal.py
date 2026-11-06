"""What a refused merge's mention says: the rule, the step that unblocks it, and for a code
owner review the files and their owners."""

import unittest
from unittest import mock

from nexkit import refusal
from nexkit.github import GitHubError
from nexkit.refusal import codeowners, explain, owners_of
from tests.support import FakeGitHub, make_config
from tests.test_report import ReportCase

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
        self.assertEqual(do, "approve the pull request as a code owner (`@olivia`), then merge it")
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
        self.assertEqual(do, "a code owner approves the pull request, then merge it")
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
                "approve the pull request, then merge it",
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
            "the reviewer who requested changes (`@olivia`) approves the pull request or "
            "dismisses the review",
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
            "approve the pull request as a code owner (`@olivia`), then merge it. GitHub "
            "requires a code owner's review for this file, and NexKit's App cannot review its "
            "own pull request: `docs/a.md` (+6 −0, owned by `@olivia`)"
        )
        self.assertEqual(outcome["attention"], reason)
        [row] = self.gh.run_comments(6)
        self.assertIn(f"**What unblocks it:** {reason}.", row["body"])
        self.assertEqual(row["run"]["attention"], reason)
        mentions = [c["body"] for c in self.gh.comments(6) if c["body"].startswith("@owner")]
        self.assertEqual(mentions, [f"@owner a person is needed: {reason}."])


if __name__ == "__main__":
    unittest.main()
