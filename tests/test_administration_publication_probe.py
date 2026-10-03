"""Probe ownership, cleanup and truthful reports with simulated GitHub APIs."""

import base64
import unittest
from contextlib import contextmanager
from copy import deepcopy
from unittest.mock import patch

from nexkit.common import Blocked
from tests.administration_publication_probe import WORKFLOW, verify
from tests.test_administrative_publisher import PUBLICATION

SOURCE = "a" * 40
HEAD = "b" * 40
ENVIRONMENT = {
    "GITHUB_SHA": SOURCE,
    "GITHUB_RUN_ID": "100",
    "GITHUB_RUN_ATTEMPT": "1",
    "GITHUB_EVENT_NAME": "workflow_dispatch",
}
PREFIX = "nexkit/publication-probe/100.1"


class ProbeGitHub:
    repository = "owner/kit"
    root = "repos/owner/kit"

    def __init__(self):
        self.refs = {"main": "c" * 40}
        self.pr = None
        self.requests = []
        self.denial = "HTTP 403"
        self.fail_after = None
        self.changed_head = False
        self.check_app = 15
        self.revoked = False
        self.revoke = True
        self.publisher_calls = 0
        self.truncated_tree = False
        self.binding = {
            **PUBLICATION,
            "repository_id": 50,
            "permissions": {"contents": "write", "workflows": "write", "metadata": "read"},
        }

    def repo(self):
        return {"id": 50, "default_branch": "main"}

    def permission(self, actor):
        return "admin" if actor == "owner" else "none"

    def ref(self, branch):
        if branch not in self.refs:
            raise Blocked("GitHub API GET /ref failed (HTTP 404)")
        return self.refs[branch]

    def content(self, path, ref):
        assert ref == HEAD and path.startswith(".github/workflows/")
        return {"content": base64.b64encode(WORKFLOW.encode()).decode()}

    def pull_for_branch(self, branch):
        return deepcopy(self.pr) if self.pr and self.pr["head"]["ref"] == branch else None

    def pull(self, number):
        assert number == 1
        return deepcopy(self.pr)

    def check(self, name, sha, passed, summary):
        assert sha == HEAD and passed and name == "NexKit publication probe"
        if self.changed_head:
            self.refs[PREFIX + "/head"] = "f" * 40
        return {
            "head_sha": sha,
            "app": {"id": self.check_app},
            "conclusion": "success",
            "html_url": "https://example.invalid/check",
        }

    def api(self, path, method="GET", data=None):
        self.requests.append(("control", path, method, deepcopy(data)))
        if "/actions/runs/" in path:
            return {
                "head_sha": SOURCE,
                "event": "workflow_dispatch",
                "path": ".github/workflows/ci.yml",
                "actor": {"login": "owner"},
            }
        if path.endswith("/git/commits/" + SOURCE):
            return {"tree": {"sha": "d" * 40}}
        if "/git/trees/" in path:
            return {"tree": [], "truncated": self.truncated_tree}
        if path.endswith("/git/trees") and method == "POST":
            if data["tree"][0]["path"].startswith(".github/workflows/"):
                raise Blocked("GitHub API POST /git/trees failed (" + self.denial + ")")
            return {"sha": "e" * 40}
        if path.endswith("/pulls") and method == "POST":
            self.pr = {
                "number": 1,
                "body": data["body"],
                "draft": True,
                "state": "open",
                "head": {"ref": data["head"], "sha": self.refs[data["head"]]},
                "base": {"ref": data["base"]},
                "user": {"login": "github-actions[bot]"},
                "html_url": "https://example.invalid/pull/1",
            }
            if self.fail_after == "pr":
                raise Blocked("Lost PR response (transport error)")
            return deepcopy(self.pr)
        if path.endswith("/pulls/1") and method == "PATCH":
            self.pr["state"] = data["state"]
            return deepcopy(self.pr)
        if path == "apps/github-actions":
            return {"id": 15}
        raise AssertionError((path, method))

    def app_api(self, path, method="GET", data=None):
        self.requests.append(("app", path, method, deepcopy(data)))
        if path == "installation/repositories":
            if self.revoked:
                raise Blocked("GitHub API GET /installation/repositories failed (HTTP 401)")
            return {"total_count": 1}
        if path.endswith("/git/trees") and method == "POST":
            return {"sha": "e" * 40}
        if path.endswith("/git/commits") and method == "POST":
            return {"sha": HEAD}
        if path.endswith("/git/refs") and method == "POST":
            branch = data["ref"].removeprefix("refs/heads/")
            self.refs[branch] = data["sha"]
            if self.fail_after == "ref":
                raise Blocked("Lost ref response (transport error)")
            return {}
        if "/git/refs/heads/" in path and method == "DELETE":
            del self.refs[path.split("/git/refs/heads/", 1)[1]]
            return None
        raise AssertionError((path, method))

    @contextmanager
    def publisher(self, gh, proposal):
        assert gh is self and proposal["publication"] == PUBLICATION
        self.publisher_calls += 1
        writer = type("Writer", (), {"binding": self.binding, "api": self.app_api})()
        try:
            yield writer
        finally:
            self.revoked = self.revoke


class PublicationProbeTests(unittest.TestCase):
    def setUp(self):
        self.gh = ProbeGitHub()
        self.enterContext(
            patch("tests.administration_publication_probe.publisher", self.gh.publisher)
        )

    def verify(self, environment=None):
        return verify(self.gh, PUBLICATION, environment or ENVIRONMENT)

    def test_exact_app_publication_and_actions_identity_clean_up_without_merge_or_model(self):
        report = self.verify()
        self.assertTrue(report["passed"], report)
        self.assertTrue(report["cleaned"] and report["token_revoked"])
        self.assertTrue(report["default_branch_preserved"])
        self.assertEqual(self.gh.refs, {"main": "c" * 40})
        self.assertEqual(self.gh.pr["state"], "closed")
        self.assertEqual(report["model_calls"], 0)
        self.assertFalse(report["consumer_delivery_verified"])
        self.assertFalse(report["native_merge_or_human_approval_verified"])
        self.assertFalse(any(path.endswith("/merge") for _, path, _, _ in self.gh.requests))

    def test_unexpected_native_denial_is_failure_without_published_refs(self):
        self.gh.denial = "HTTP 401"
        report = self.verify()
        self.assertFalse(report["passed"], report)
        self.assertEqual(self.gh.refs, {"main": "c" * 40})
        self.assertIsNone(self.gh.pr)
        self.assertTrue(self.gh.revoked)

    def test_lost_ref_or_pr_responses_cleanup_only_the_owned_resources(self):
        for operation in ("ref", "pr"):
            with self.subTest(operation=operation):
                self.gh.fail_after = operation
                self.gh.pr = None
                report = self.verify()
                self.assertFalse(report["passed"], report)
                self.assertTrue(report["cleaned"])
                self.assertEqual(self.gh.refs, {"main": "c" * 40})
                if operation == "pr":
                    self.assertEqual(self.gh.pr["state"], "closed")
                self.assertTrue(self.gh.revoked)

    def test_changed_head_is_preserved_instead_of_deleted(self):
        self.gh.changed_head = True
        report = self.verify()
        self.assertFalse(report["passed"], report)
        self.assertFalse(report["cleaned"])
        self.assertEqual(self.gh.refs[PREFIX + "/head"], "f" * 40)
        self.assertEqual(self.gh.pr["state"], "open")
        self.assertTrue(self.gh.revoked)

    def test_revocation_failure_cannot_be_reported_as_passing(self):
        self.gh.revoke = False
        report = self.verify()
        self.assertFalse(report["passed"], report)
        self.assertTrue(report["cleaned"])
        self.assertFalse(report["token_revoked"])

    def test_other_check_identity_cannot_satisfy_publication_evidence(self):
        self.gh.check_app = 999
        report = self.verify()
        self.assertFalse(report["passed"], report)
        self.assertTrue(report["cleaned"])
        self.assertFalse(report["live_github_publication"])

    def test_existing_refs_and_wrong_events_block_before_app_or_writes(self):
        self.gh.refs[PREFIX + "/base"] = "f" * 40
        self.assertFalse(self.verify()["passed"])
        self.assertEqual(self.gh.publisher_calls, 0)
        self.assertEqual(self.gh.refs[PREFIX + "/base"], "f" * 40)
        self.assertFalse(
            self.verify({**ENVIRONMENT, "GITHUB_EVENT_NAME": "pull_request"})["passed"]
        )
        self.assertFalse(any(method != "GET" for _, _, method, _ in self.gh.requests))

    def test_partial_source_inventory_cannot_authorize_a_fixture_write(self):
        self.gh.truncated_tree = True
        self.assertFalse(self.verify()["passed"])
        self.assertEqual(self.gh.publisher_calls, 0)
        self.assertFalse(any(method != "GET" for _, _, method, _ in self.gh.requests))

    def test_report_records_exact_owned_resources_before_a_lost_write_response(self):
        self.gh.fail_after = "ref"
        records = []
        report = verify(
            self.gh,
            PUBLICATION,
            ENVIRONMENT,
            record=lambda value: records.append(deepcopy(value)),
        )
        self.assertFalse(report["passed"])
        self.assertTrue(
            any(
                value.get("candidate_head") == HEAD
                and value["branches"] == [PREFIX + "/base", PREFIX + "/head"]
                and value["source_commit"] == SOURCE
                for value in records
            )
        )
        self.assertEqual(records[-1], report)


if __name__ == "__main__":
    unittest.main()
