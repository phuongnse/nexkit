import unittest
from unittest import mock

from nexkit.github import GitHub, GitHubError


def editors(nodes, more=False, events=()):
    """`issue_editors` with GitHub answering `nodes` and the issue's `events`."""
    gh = GitHub(repository="acme/app", token="t")
    edits = {"nodes": nodes, "pageInfo": {"hasNextPage": more}}
    data = {"repository": {"issue": {"userContentEdits": edits}}}
    with (
        mock.patch.object(gh, "graphql", return_value=data) as query,
        mock.patch.object(gh, "paginate", return_value=list(events)),
    ):
        found = gh.issue_editors(5)
    query.assert_called_once()
    return found


class IssueEditorsTests(unittest.TestCase):
    def test_a_bot_is_named_as_rest_names_it(self):
        nodes = [
            {"editor": {"__typename": "Bot", "login": "github-actions"}},
            {"editor": {"__typename": "User", "login": "alice"}},
        ]
        self.assertEqual(editors(nodes), ["alice", "github-actions[bot]"])

    def test_someone_github_cannot_name_counts(self):
        self.assertEqual(editors([{"editor": None}]), [""])
        self.assertEqual(editors([], more=True), [""])

    def test_a_new_title_counts_as_an_edit(self):
        events = [
            {"event": "renamed", "actor": {"login": "bob"}},
            {"event": "labeled", "actor": {"login": "carol"}},
        ]
        self.assertEqual(editors([], events=events), ["bob"])


class IssueEditsTests(unittest.TestCase):
    def edits(self, nodes, more=False, events=()):
        gh = GitHub(repository="acme/app", token="t")
        data = {
            "repository": {
                "issue": {"userContentEdits": {"nodes": nodes, "pageInfo": {"hasNextPage": more}}}
            }
        }
        with (
            mock.patch.object(gh, "graphql", return_value=data),
            mock.patch.object(gh, "paginate", return_value=list(events)),
        ):
            return gh.issue_edits(5)

    def test_each_edit_has_its_time(self):
        nodes = [
            {"editedAt": "2026-01-02T00:00:00Z", "editor": {"__typename": "User", "login": "bob"}},
            {"editedAt": "2026-01-01T00:00:00Z", "editor": {"__typename": "Bot", "login": "ci"}},
        ]
        self.assertEqual(
            self.edits(nodes),
            [("bob", "2026-01-02T00:00:00Z"), ("ci[bot]", "2026-01-01T00:00:00Z")],
        )

    def test_a_new_title_has_its_time(self):
        events = [
            {"event": "renamed", "actor": {"login": "bob"}, "created_at": "2026-01-03T00:00:00Z"},
            {"event": "labeled", "actor": {"login": "carol"}, "created_at": "2026-01-04T00:00:00Z"},
        ]
        self.assertEqual(self.edits([], events=events), [("bob", "2026-01-03T00:00:00Z")])

    def test_edits_past_what_github_lists_have_no_time(self):
        self.assertEqual(self.edits([], more=True), [("", None)])


class BranchTests(unittest.TestCase):
    def setUp(self):
        self.gh = GitHub(repository="acme/app", token="t")

    def test_a_branch_that_is_gone_already_is_no_failure(self):
        for error in (GitHubError(404, "Not Found"), GitHubError(422, "Reference does not exist")):
            with mock.patch.object(self.gh, "request", side_effect=error):
                self.gh.delete_branch("nexkit/issue-5")

    def test_a_branch_that_a_rule_keeps_is_a_failure(self):
        error = GitHubError(422, "Repository rule violations found")
        with mock.patch.object(self.gh, "request", side_effect=error):
            with self.assertRaises(GitHubError):
                self.gh.delete_branch("nexkit/issue-5")

    def test_it_lists_branches_by_prefix_with_their_heads(self):
        refs = [{"ref": "refs/heads/nexkit/issue-5", "object": {"sha": "a" * 40}}]
        with mock.patch.object(self.gh, "paginate", return_value=refs) as paginate:
            found = self.gh.branches_like("nexkit/issue-")
        self.assertEqual(found, [("nexkit/issue-5", "a" * 40)])
        self.assertIn("/git/matching-refs/heads/nexkit/issue-", paginate.call_args.args[0])

    def test_it_lists_the_pull_requests_of_a_branch_in_any_state(self):
        with mock.patch.object(self.gh, "paginate", return_value=[]) as paginate:
            self.gh.head_pulls("nexkit/issue-5")
        self.assertEqual(
            paginate.call_args.args[1], {"state": "all", "head": "acme:nexkit/issue-5"}
        )


class RefusalReadsTests(unittest.TestCase):
    def setUp(self):
        self.gh = GitHub(repository="acme/app", token="t")

    def test_review_state_counts_unresolved_conversations(self):
        threads = {"nodes": [{"isResolved": True}, {"isResolved": False}, None]}
        pull = {"isDraft": False, "reviewDecision": "REVIEW_REQUIRED", "reviewThreads": threads}
        data = {"repository": {"pullRequest": pull}}
        with mock.patch.object(self.gh, "graphql", return_value=data):
            self.assertEqual(
                self.gh.review_state(6),
                {"draft": False, "decision": "REVIEW_REQUIRED", "unresolved": 1},
            )

    def test_file_text_reads_a_file_and_none_for_a_missing_or_large_one(self):
        found = {"encoding": "base64", "content": "KiBAb2xpdmlh\n"}
        with mock.patch.object(self.gh, "request", return_value=found) as request:
            self.assertEqual(self.gh.file_text("CODEOWNERS", "main"), "* @olivia")
        request.assert_called_once_with(
            "GET", "/repos/acme/app/contents/CODEOWNERS", params={"ref": "main"}
        )
        with mock.patch.object(self.gh, "request", return_value={"encoding": "none"}):
            self.assertIsNone(self.gh.file_text("CODEOWNERS", "main"))
        with mock.patch.object(self.gh, "request", side_effect=GitHubError(404, "Not Found")):
            self.assertIsNone(self.gh.file_text("CODEOWNERS", "main"))
        with mock.patch.object(self.gh, "request", side_effect=GitHubError(500, "x")):
            with self.assertRaises(GitHubError):
                self.gh.file_text("CODEOWNERS", "main")
