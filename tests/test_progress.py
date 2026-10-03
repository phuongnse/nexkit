"""Bot progress notices follow committed state; never control work or usage."""

import base64
import json
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import yaml

from nexkit.common import Blocked
from nexkit.github import GitHub
from nexkit.progress import sync

ENV = {
    "NEXKIT_PROGRESS": "1",
    "GITHUB_ACTIONS": "true",
    "GITHUB_REPOSITORY": "owner/project",
    "GITHUB_RUN_ID": "101",
    "GITHUB_RUN_ATTEMPT": "2",
    "GITHUB_STEP_SUMMARY": "",
}


class NoticeGitHub(GitHub):
    def __init__(self):
        super().__init__("owner/project")
        self.state = {
            "status": "working",
            "activity": "Inspecting app.py",
            "run_key": "101.1",
            "calls": 1,
        }
        self.notices = []
        self.artifacts = []
        self.writes = []
        self.denied = False
        self.race = None
        self.lost_response = False

    def ensure_state_branch(self):
        pass

    def get_state(self, number):
        return deepcopy(self.state), "state-sha"

    def comments(self, number):
        return deepcopy(self.notices)

    def api(self, path, method="GET", data=None, **kwargs):
        if method == "PUT":
            self.state = json.loads(base64.b64decode(data["content"]))
            self.writes.append((method, deepcopy(self.state)))
            return {"content": {"sha": "next-sha"}}
        if self.denied:
            raise Blocked("HTTP 403 diagnostic permission unavailable")
        if "/artifacts?" in path:
            return self.artifacts
        if method == "POST":
            self.notices.append(
                {
                    "id": len(self.notices) + 1,
                    "body": data["body"],
                    "user": {"login": "github-actions[bot]", "type": "Bot"},
                }
            )
        elif method == "PATCH":
            next(item for item in self.notices if item["id"] == int(path.split("/")[-1]))[
                "body"
            ] = data["body"]
        elif method == "DELETE":
            self.notices = [item for item in self.notices if item["id"] != int(path.split("/")[-1])]
            self.writes.append((method, path))
            return {}
        else:
            raise AssertionError((path, method))
        self.writes.append((method, data["body"]))
        if self.race:
            self.state.update(self.race)
            self.race = None
        if self.lost_response:
            self.lost_response = False
            raise Blocked("HTTP response lost after comment creation")
        return self.notices[-1]


class ProgressTests(unittest.TestCase):
    def test_concurrent_creation_converges_to_one_canonical_bot_notice(self):
        gh = NoticeGitHub()
        barrier = threading.Barrier(2)
        lock = threading.Lock()
        reads = 0

        def comments(number):
            nonlocal reads
            with lock:
                reads += 1
                captured = deepcopy(gh.notices)
                initial = reads <= 2
            if initial:
                barrier.wait(timeout=5)
            return captured

        with patch.dict(os.environ, ENV), patch.object(gh, "comments", side_effect=comments):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(sync, gh, 17) for _ in range(2)]
                for future in futures:
                    future.result(timeout=10)
        self.assertEqual([write[0] for write in gh.writes].count("POST"), 2)
        self.assertEqual(len(gh.notices), 1)
        self.assertEqual(gh.notices[0]["id"], 1)
        self.assertEqual(gh.state["calls"], 1)

    def test_one_bot_notice_updates_and_ignores_a_copied_human_marker(self):
        gh = NoticeGitHub()
        gh.notices.append(
            {
                "id": 1,
                "body": "<!-- nexkit:progress:17 -->\nHuman copy",
                "user": {"login": "collaborator", "type": "User"},
            }
        )
        with patch.dict(os.environ, ENV):
            gh.save_state(17, gh.state, "state-sha")
            sync(gh, 17)
            gh.state["status"] = "completed"
            gh.state["activity"] = "Report complete"
            gh.save_state(17, gh.state, "next-sha")
        self.assertEqual(len(gh.notices), 2)
        self.assertEqual(gh.notices[0]["body"], "<!-- nexkit:progress:17 -->\nHuman copy")
        self.assertIn("Report complete", gh.notices[1]["body"])
        self.assertIn("/runs/101/attempts/2", gh.notices[1]["body"])
        self.assertEqual([w[0] for w in gh.writes].count("POST"), 1)
        self.assertEqual(gh.state["calls"], 1)

    def test_lost_comment_response_is_recovered_without_duplicate_or_new_reservation(self):
        gh = NoticeGitHub()
        gh.lost_response = True
        with patch.dict(os.environ, ENV):
            self.assertEqual(gh.save_state(17, gh.state, "state-sha"), "next-sha")
            sync(gh, 17)
        self.assertEqual(len(gh.notices), 1)
        self.assertEqual(gh.state["calls"], 1)

    def test_state_change_during_notice_is_refreshed_without_stale_regression(self):
        gh = NoticeGitHub()
        gh.race = {"status": "completed", "activity": "Newest completed state"}
        with patch.dict(os.environ, ENV):
            gh.save_state(17, gh.state, "state-sha")
        self.assertEqual(len(gh.notices), 1)
        self.assertIn("Newest completed state", gh.notices[0]["body"])

    def test_optional_diagnostic_failure_does_not_undo_committed_state(self):
        gh = NoticeGitHub()
        gh.denied = True
        with patch.dict(os.environ, ENV):
            self.assertEqual(gh.save_state(17, gh.state, "state-sha"), "next-sha")
        self.assertEqual(gh.writes[0][0], "PUT")
        self.assertEqual(gh.state["calls"], 1)
        self.assertEqual(gh.notices, [])

    def test_native_identity_opt_in_and_repository_are_required_for_publication(self):
        for changes in (
            {"NEXKIT_PROGRESS": ""},
            {"GITHUB_ACTIONS": "false"},
            {"GITHUB_REPOSITORY": "different/project"},
            {"GITHUB_RUN_ID": "invalid"},
        ):
            with self.subTest(changes=changes), patch.dict(os.environ, {**ENV, **changes}):
                gh = NoticeGitHub()
                gh.save_state(17, gh.state, "state-sha")
                self.assertEqual(gh.notices, [])
                self.assertNotIn("progress", gh.state)

    def test_only_current_attempt_report_artifacts_are_linked_and_secrets_are_hidden(self):
        gh = NoticeGitHub()
        gh.state["run_key"] = "101.2"
        gh.state["activity"] = "known-secret @everyone <script>"
        gh.artifacts = [
            {"id": 1, "name": "agent-diagnostics-task-inspect-2", "expired": False},
            {"id": 2, "name": "agent-diagnostics-task-inspect-1", "expired": False},
            {"id": 3, "name": "agent-diagnostics-task-inspect-2", "expired": True},
            {"id": 4, "name": "unrelated-private-artifact-2", "expired": False},
        ]
        with patch.dict(os.environ, {**ENV, "GH_TOKEN": "known-secret"}):
            gh.save_state(17, gh.state, "state-sha")
        text = gh.notices[0]["body"]
        self.assertIn("/artifacts/1)", text)
        for value in (
            "/artifacts/2)",
            "/artifacts/3)",
            "/artifacts/4)",
            "known-secret",
            "@everyone",
            "<script>",
        ):
            self.assertNotIn(value, text)

    def test_diagnostics_follow_every_timed_session_and_execution_jobs_keep_read_permissions(self):
        root = Path(__file__).resolve().parents[1]
        sessions = 0
        for path in (root / ".github/workflows").glob("*.yml"):
            workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
            for job in workflow.get("jobs", {}).values():
                steps = job.get("steps", [])
                for index, step in enumerate(steps):
                    if step.get("uses") != "./kit/actions/agent-session":
                        continue
                    sessions += 1
                    cleanup = steps[index + 1]
                    self.assertEqual(cleanup["uses"], "./kit/actions/agent-diagnostics")
                    self.assertEqual(cleanup["if"], "always()")
                    self.assertTrue(cleanup["continue-on-error"])
                    self.assertIn("timeout-minutes", step)
                    self.assertNotIn("write", job["permissions"].values())
        self.assertEqual(sessions, 5)
