import io
import json
import unittest
from unittest import mock

from nexkit.github import LOG_CUT, GitHub, GitHubError


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

    def test_a_run_s_artifacts_are_read_page_by_page(self):
        pages = [
            {"artifacts": [{"id": n} for n in range(100)]},
            {"artifacts": [{"id": 100}]},
        ]
        with mock.patch.object(self.gh, "request", side_effect=pages) as request:
            self.assertEqual(len(self.gh.run_artifacts(7)), 101)
        self.assertEqual(request.call_args.kwargs["params"], {"per_page": 100, "page": 2})


class TransportTests(unittest.TestCase):
    def test_a_read_that_times_out_is_tried_again_then_fails_as_github(self):
        gh = GitHub(repository="acme/app", token="t")
        answer = mock.MagicMock()
        answer.__enter__.return_value.read.return_value = b'{"ok": true}'
        with (
            mock.patch("nexkit.github.urlopen", side_effect=[TimeoutError("timed out"), answer]),
            mock.patch("nexkit.github.time.sleep") as sleep,
        ):
            self.assertEqual(gh.request("GET", "/x"), {"ok": True})
        sleep.assert_called_once()
        with (
            mock.patch("nexkit.github.urlopen", side_effect=ConnectionResetError("reset")),
            mock.patch("nexkit.github.time.sleep"),
            self.assertRaises(GitHubError) as caught,
        ):
            gh.request("GET", "/x")
        self.assertEqual(caught.exception.status, 0)


def http_error(code, body):
    from io import BytesIO
    from urllib.error import HTTPError

    return HTTPError("https://api.github.com/x", code, "error", {}, BytesIO(body.encode()))


class RetryTests(unittest.TestCase):
    def answer(self):
        answer = mock.MagicMock()
        answer.__enter__.return_value.read.return_value = b'{"number": 7}'
        return answer

    def test_a_server_error_or_rate_limit_is_tried_again_after_each_wait(self):
        gh = GitHub(repository="acme/app", token="t")
        gh.waits = (10, 30, 60)
        errors = [
            http_error(502, "Bad Gateway"),
            http_error(429, "Too Many Requests"),
            http_error(403, "You have exceeded a secondary rate limit."),
            self.answer(),
        ]
        with (
            mock.patch("nexkit.github.urlopen", side_effect=errors),
            mock.patch("nexkit.github.time.sleep") as sleep,
        ):
            self.assertEqual(gh.request("POST", "/x", {}), {"number": 7})
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [10, 30, 60])

    def test_every_try_failing_is_a_transient_error(self):
        gh = GitHub(repository="acme/app", token="t")
        with (
            mock.patch("nexkit.github.urlopen", side_effect=[http_error(500, "x")] * 3),
            mock.patch("nexkit.github.time.sleep") as sleep,
            self.assertRaises(GitHubError) as caught,
        ):
            gh.request("POST", "/x", {})
        self.assertEqual((caught.exception.status, caught.exception.transient), (500, True))
        self.assertEqual(sleep.call_count, 2)

    def test_a_refusal_fails_at_once(self):
        gh = GitHub(repository="acme/app", token="t")
        for code, body in ((403, "Resource not accessible by integration"), (422, "exists")):
            with (
                mock.patch("nexkit.github.urlopen", side_effect=[http_error(code, body)]),
                mock.patch("nexkit.github.time.sleep") as sleep,
                self.assertRaises(GitHubError) as caught,
            ):
                gh.request("POST", "/x", {})
            self.assertFalse(caught.exception.transient)
            sleep.assert_not_called()


class CommentRetryTests(unittest.TestCase):
    def answer(self, data):
        answer = mock.MagicMock()
        answer.__enter__.return_value.read.return_value = json.dumps(data).encode()
        return answer

    def post(self, *answers):
        gh = GitHub(repository="acme/app", token="t")
        with (
            mock.patch("nexkit.github.urlopen", side_effect=list(answers)) as opened,
            mock.patch("nexkit.github.time.sleep"),
        ):
            found = gh.comment(5, "## Plan")
        return found, [call.args[0].get_method() for call in opened.call_args_list]

    def test_a_comment_github_posted_on_a_failed_try_is_not_posted_again(self):
        mine = {"id": 9, "body": "## Plan"}
        listed = self.answer([{"id": 8, "body": "Other"}, mine])
        found, methods = self.post(http_error(502, "Bad Gateway"), listed)
        self.assertEqual(found, mine)
        self.assertEqual(methods, ["POST", "GET"])

    def test_a_comment_github_did_not_post_is_tried_again(self):
        posted = {"id": 9, "body": "## Plan"}
        found, methods = self.post(
            http_error(503, "Unavailable"), self.answer([]), self.answer(posted)
        )
        self.assertEqual(found, posted)
        self.assertEqual(methods, ["POST", "GET", "POST"])

    def test_a_refused_comment_fails_at_once(self):
        with self.assertRaises(GitHubError):
            self.post(http_error(403, "Resource not accessible by integration"))


class DownloadTests(unittest.TestCase):
    def download(self, data, call):
        """What `call` returns when the storage a download goes to answers `data`."""
        gh = GitHub(repository="acme/app", token="t")
        with mock.patch("nexkit.github.build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(data)
            return call(gh)

    def test_a_job_log_longer_than_nexkit_reads_keeps_its_end_and_says_it_was_cut(self):
        lines = [f"2026-10-23T03:30:00.0Z line {n}\n".encode() for n in range(100_000)]
        data = b"".join(lines) + b"2026-10-23T03:30:09.0Z ##[error]Process completed\n"
        # Read in parts, more than one of which is longer than what is kept.
        limit = 1_500_000
        self.assertGreater(len(data), 2 * limit)
        log = self.download(data, lambda gh: gh.job_log(7, limit))
        self.assertTrue(log.startswith(LOG_CUT))
        end = log.removeprefix(LOG_CUT)
        self.assertLessEqual(len(end), limit)
        self.assertTrue(data.endswith(end))
        self.assertTrue(end.startswith(b"2026-10-23T03:30:00.0Z line "))
        self.assertGreater(len(end), limit - 100)
        # A log within the limit is whole, and an artifact's zip keeps its start.
        self.assertEqual(self.download(data, lambda gh: gh.job_log(7)), data)
        zipped = self.download(data, lambda gh: gh.artifact_zip(8, 100))
        self.assertEqual(zipped, data[:101])
