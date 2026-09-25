import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Lock

from nexkit.cli import create_request, intake_status, submit
from tests.support import project


class IntakeGitHub:
    repository = "owner/project"
    root = "repos/owner/project"

    def __init__(self):
        self.issues = []
        self.dispatched = []

    def dispatch(self, workflow, branch, inputs):
        self.dispatched.append((workflow, branch, inputs))

    def api(self, path, method="GET", data=None, **kwargs):
        if method == "GET":
            return deepcopy(self.issues)
        issue = {
            **data,
            "number": len(self.issues) + 1,
            "html_url": "https://example.test/issues/1",
        }
        self.issues.append(issue)
        return deepcopy(issue)


class IntakeTests(unittest.TestCase):
    def test_public_intake_only_dispatches_and_never_races_issue_creation(self):
        gh = IntakeGitHub()
        payload = {"title": "Feature", "request": "Desired behavior", "key": "stable-key"}
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: submit(gh, project(), "request", payload), range(2)))
        self.assertTrue(all(r["queued"] for r in results))
        self.assertEqual(gh.issues, [])
        self.assertEqual(len(gh.dispatched), 2)
        # Simulate the native Actions concurrency guarantee, not GitHub itself.
        native_queue = Lock()

        def run_queued(_):
            with native_queue:
                return create_request(gh, payload["title"], payload["request"], payload["key"])

        with ThreadPoolExecutor(2) as pool:
            created = list(pool.map(run_queued, range(2)))
        self.assertEqual(len(gh.issues), 1)
        self.assertEqual(sorted(r["created"] for r in created), [False, True])

    def test_retry_after_work_item_creation_recovers_same_issue(self):
        gh = IntakeGitHub()
        first = create_request(gh, "Feature", "Expected behavior", "stable-key")
        second = create_request(gh, "Feature", "Expected behavior", "stable-key")
        self.assertEqual(first["issue"]["number"], second["issue"]["number"])
        self.assertEqual(len(gh.issues), 1)

    def test_lookup_follows_async_submission_without_creating_another_issue(self):
        gh = IntakeGitHub()
        self.assertFalse(intake_status(gh, "request", "stable-key")["found"])
        issue = create_request(gh, "Feature", "Expected behavior", "stable-key")["issue"]
        found = intake_status(gh, "request", "stable-key")
        self.assertEqual(found["issue"], issue)
        self.assertEqual(len(gh.issues), 1)
