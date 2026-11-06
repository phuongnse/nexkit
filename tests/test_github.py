import unittest
from unittest import mock

from nexkit.github import GitHub


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
