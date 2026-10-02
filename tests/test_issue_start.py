"""Existing-issue intake with simulated GitHub boundaries, never live approvals."""

import base64
import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from nexkit import clarify, cli, intake
from nexkit.ci import prepare_job
from nexkit.common import Blocked, read_json, write_json
from nexkit.delivery import prepare as prepare_delivery
from nexkit.pipelines import effective_config, issue_pipeline
from nexkit.policy import now, spec_hash
from tests.support import FakeGitHub, approve
from tests.test_clarify import result as clarification_result
from tests.test_pipelines import composed


class ExistingIssueGitHub(FakeGitHub):
    """Exercise real controllers while all remote reads and writes stay local."""

    def __init__(self, *, clarification=True):
        super().__init__()
        self.discussion = []
        self.permissions = {"owner": "admin", "teammate": "write", "maintainer": "maintain"}
        self.patches = []
        self.default_branch = "main"
        self.work["body"] = "CLI must sum negative and positive integers."
        self.cfg, self.files = composed()
        if clarification:
            path = ".github/workflows/discuss.yml"
            self.files[path] = b"name: Discuss\non: workflow_dispatch\njobs: {}\n"
            self.cfg["files"][path] = {
                "sha256": hashlib.sha256(self.files[path]).hexdigest(),
                "managed": True,
            }
            self.cfg["pipelines"]["maintenance"]["entrypoints"]["clarify"] = path
            self.cfg["pipelines"]["maintenance"]["agent_workflows"].append(path)
        self.cfg["pipelines"]["audit"]["entrypoints"] = {"intake": ".github/workflows/capture.yaml"}

    def repo(self):
        return {"default_branch": self.default_branch}

    def permission(self, login):
        return self.permissions.get(login, "read")

    def content(self, path, ref):
        return {
            "type": "file",
            "encoding": "base64",
            "content": base64.b64encode(self.files[path]).decode(),
        }

    def api(self, path, method="GET", data=None, **kwargs):
        if path == f"{self.root}/issues/1":
            if method == "PATCH":
                self.patches.append(deepcopy(data))
                self.work.update(data, last_edited_at=now())
            else:
                assert method == "GET"
            return deepcopy(self.work)
        if path.startswith(f"{self.root}/issues/comments/") and method == "GET":
            number = int(path.rsplit("/", 1)[1])
            for comment in self.discussion:
                if comment["id"] == number:
                    return deepcopy(comment)
            raise Blocked("HTTP 404: comment deleted")
        # There is deliberately no issue-creation mock for this path. Any
        # accidental POST /issues fails the test instead of hiding a duplicate.
        return super().api(path, method, data, **kwargs)

    def add_comment(self, body, *, login="owner", user_type="User"):
        stamp = now()
        comment = {
            "id": len(self.discussion) + 100,
            "body": body,
            "issue_url": f"https://api.github.com/{self.root}/issues/1",
            "created_at": stamp,
            "updated_at": stamp,
            "user": {"login": login, "type": user_type, "id": 30 if login == "owner" else 31},
        }
        self.discussion.append(comment)
        return {
            "action": "created",
            "comment": deepcopy(comment),
            "issue": deepcopy(self.work),
            "sender": deepcopy(comment["user"]),
        }

    def comment(self, number, body):
        self.messages.append(body)
        return self.add_comment(body, login="github-actions[bot]", user_type="Bot")["comment"]


class ExistingIssueTests(unittest.TestCase):
    def setUp(self):
        self.gh = ExistingIssueGitHub()
        self.env = {
            "GITHUB_REPOSITORY": self.gh.repository,
            "GITHUB_ACTOR": "owner",
            "GITHUB_EVENT_NAME": "issue_comment",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_WORKFLOW_REF": "owner/project/.github/workflows/capture.yaml@refs/heads/main",
            "GITHUB_WORKFLOW_SHA": "b" * 40,
            "GITHUB_RUN_ID": "100",
            "NEXKIT_PIPELINE": "",
        }
        self.environment = patch.dict(os.environ, self.env)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def start(self, body="/nexkit start maintenance", **kwargs):
        event = self.gh.add_comment(body, **kwargs)
        return event, intake.receive(self.gh, event)

    def assert_no_effects(self, original):
        self.assertEqual(self.gh.work, original)
        self.assertEqual(self.gh.patches, [])
        self.assertEqual(self.gh.dispatches, [])
        self.assertEqual(self.gh.messages, [])
        self.assertEqual(self.gh.state, {})
        self.assertIsNone(self.gh.pr)

    def run_main(self, event):
        with tempfile.TemporaryDirectory() as directory:
            event_path = Path(directory) / "event.json"
            summary = Path(directory) / "summary.md"
            write_json(event_path, event)
            with (
                patch.dict(
                    os.environ,
                    {"GITHUB_EVENT_PATH": str(event_path), "GITHUB_STEP_SUMMARY": str(summary)},
                ),
                patch("nexkit.intake.GitHub", return_value=self.gh),
                redirect_stdout(io.StringIO()) as output,
                redirect_stderr(io.StringIO()) as errors,
            ):
                status = intake.main()
            return (
                status,
                output.getvalue(),
                errors.getvalue(),
                summary.read_text() if summary.exists() else "",
            )

    def test_comment_adopts_same_issue_preserves_request_and_only_dispatches_clarification(self):
        self.gh.work["body"] = (
            "## Expected behavior\n\n- Handle -2 and 5.\n- Print `3`.\n\nKeep $(literal) text."
        )
        self.gh.add_comment("Keep the current command name.", login="teammate")
        original, discussion = deepcopy(self.gh.work), deepcopy(self.gh.discussion)
        _, result = self.start()
        self.assertTrue(result["adopted"])
        self.assertFalse(result["created"])
        for key in ("number", "html_url", "title", "state"):
            self.assertEqual(result["issue"][key], original[key])
        preserved = (
            self.gh.work["body"]
            .split("<summary>Original request</summary>\n\n", 1)[1]
            .split("\n\n</details>\n", 1)[0]
        )
        self.assertEqual(
            "\n".join(line.removeprefix("> ") for line in preserved.splitlines()), original["body"]
        )
        self.assertEqual(self.gh.discussion[: len(discussion)], discussion)
        self.assertEqual(issue_pipeline(self.gh.work), "maintenance")
        self.assertEqual(
            self.gh.dispatches, [("discuss.yml", "main", {"issue": 1, "pipeline": "maintenance"})]
        )
        self.assertEqual(len(self.gh.patches), 1)
        self.assertEqual(len(self.gh.messages), 1)
        self.assertNotIn("human", self.gh.messages[0])
        self.assertEqual(self.gh.state, {})
        self.assertIsNone(self.gh.pr)

    def test_current_issue_body_is_used_instead_of_the_queued_event_snapshot(self):
        event = self.gh.add_comment("/nexkit start maintenance")
        self.gh.work["body"] = "Updated requirement before the queued run starts."
        result = intake.receive(self.gh, event)
        self.assertIn(
            "> Updated requirement before the queued run starts.", result["issue"]["body"]
        )

    def test_manual_new_request_posts_intake_instructions_and_recovers_without_duplicates(self):
        self.gh = ExistingIssueGitHub(clarification=False)
        self.gh.work["body"] = cli.request_body("Run checks", "checks", pipeline="maintenance")
        event = {
            "inputs": {
                "operation": "request",
                "pipeline": "maintenance",
                "payload": json.dumps(
                    {"title": "Run checks", "request": "Run checks", "key": "checks"}
                ),
            }
        }
        with (
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "workflow_dispatch"}),
            patch(
                "nexkit.intake.create_request",
                return_value={"created": True, "issue": deepcopy(self.gh.work)},
            ),
        ):
            intake.receive(self.gh, event)
            intake.receive(self.gh, event)
        self.assertEqual(len(self.gh.messages), 1)
        self.assertIn("manually prepared specification", self.gh.messages[0])
        self.assertIn("nexkit spec 1", self.gh.messages[0])
        self.assertEqual(self.gh.dispatches, [])
        self.assertEqual(self.gh.state, {})

    def test_cli_start_queues_existing_issue_without_reading_or_creating_it(self):
        with tempfile.TemporaryDirectory() as directory:
            gh = ExistingIssueGitHub()
            root = Path(directory)
            write_json(root / ".nexkit/project.json", gh.cfg)
            args = ["--root", directory] + ["--pipeline", "maintenance"] + ["start", "1"]
            with (
                patch("nexkit.cli.GitHub", return_value=gh),
                redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(cli.main(args), 0)
            result = json.loads(output.getvalue())
            self.assertTrue(result["queued"])
            self.assertEqual(result["issue"], 1)
            workflow, branch, inputs = gh.dispatches[0]
            self.assertEqual(workflow, "capture.yaml")
            self.assertEqual(branch, "main")
            self.assertEqual(inputs["operation"], "start")
            self.assertEqual(json.loads(inputs["payload"]), {"issue": 1})
            self.assertEqual(gh.patches, [])
            self.assertEqual(gh.messages, [])

    def test_native_dispatch_supports_the_cli_payload(self):
        event = {
            "inputs": {"operation": "start", "pipeline": "maintenance", "payload": '{"issue":1}'}
        }
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "workflow_dispatch"}):
            self.assertTrue(intake.receive(self.gh, event)["adopted"])
        self.assertEqual(issue_pipeline(self.gh.work), "maintenance")

    def test_start_comment_requires_an_explicit_pipeline(self):
        original = deepcopy(self.gh.work)
        with self.assertRaisesRegex(Blocked, "Use /nexkit start PIPELINE"):
            self.start("/nexkit start")
        self.assert_no_effects(original)

    def test_pipeline_without_clarification_provides_manual_spec_instructions(self):
        self.gh = ExistingIssueGitHub(clarification=False)
        _, result = self.start()
        self.assertTrue(result["adopted"])
        self.assertEqual(self.gh.dispatches, [])
        self.assertIn("nexkit spec 1", self.gh.messages[0])
        self.assertIn("nexkit approval 1", self.gh.messages[0])

    def test_shared_entrypoint_can_select_another_configured_pipeline(self):
        _, result = self.start("/nexkit start audit")
        self.assertTrue(result["adopted"])
        self.assertEqual(issue_pipeline(self.gh.work), "audit")
        self.assertEqual(self.gh.dispatches, [])

    def test_fixed_entrypoint_skips_another_pipeline(self):
        original = deepcopy(self.gh.work)
        with patch.dict(os.environ, {"NEXKIT_PIPELINE": "maintenance"}):
            _, result = self.start("/nexkit start audit")
        self.assertTrue(result["skipped"])
        self.assert_no_effects(original)

    def test_invalid_unknown_and_missing_pipeline_commands_do_not_mutate_the_issue(self):
        original = deepcopy(self.gh.work)
        for body in (
            "/nexkit start missing",
            "/nexkit start",
            "/nexkit start maintenance extra",
            "/nexkit start maintenance\n/run anything",
            "/nexkit start $(anything)",
        ):
            with self.subTest(body=body), self.assertRaises(Blocked):
                self.start(body)
            self.assert_no_effects(original)

    def test_unrelated_edited_deleted_and_pr_events_do_not_start_intake(self):
        original = deepcopy(self.gh.work)
        event = self.gh.add_comment("/nexkit start maintenance")
        variants = [
            {**event, "action": "edited"},
            {**event, "action": "deleted"},
            {**event, "issue": {**event["issue"], "pull_request": {"url": "example"}}},
            {**event, "comment": {**event["comment"], "body": "An ordinary answer"}},
        ]
        for item in variants:
            with self.subTest(item=item):
                self.assertTrue(intake.receive(self.gh, item)["skipped"])
                self.assert_no_effects(original)

    def test_all_current_collaborator_roles_can_start_without_named_account_policy(self):
        for login in ("owner", "teammate", "maintainer"):
            with self.subTest(login=login):
                self.gh = ExistingIssueGitHub()
                with patch.dict(os.environ, {"GITHUB_ACTOR": login}):
                    _, result = self.start(login=login)
                self.assertTrue(result["adopted"])

    def test_outsider_and_bot_commands_cannot_mutate_or_dispatch(self):
        original = deepcopy(self.gh.work)
        for login, kind in (("outsider", "User"), ("owner", "Bot")):
            with self.subTest(login=login, kind=kind), self.assertRaises(Blocked):
                self.start(login=login, user_type=kind)
            self.assert_no_effects(original)

    def test_changed_deleted_and_cross_issue_commands_are_rejected(self):
        for change in ("body", "edit-revert", "deleted", "issue", "actor"):
            with self.subTest(change=change):
                self.gh = ExistingIssueGitHub()
                original = deepcopy(self.gh.work)
                event = self.gh.add_comment("/nexkit start maintenance")
                comment = self.gh.discussion[0]
                if change == "body":
                    comment["body"] = "/nexkit start audit"
                elif change == "edit-revert":
                    comment["updated_at"] = "2099-01-01T00:00:00Z"
                elif change == "deleted":
                    self.gh.discussion.clear()
                elif change == "issue":
                    comment["issue_url"] = "https://api.github.com/repos/owner/project/issues/2"
                else:
                    comment["user"]["id"] += 1
                with self.assertRaises(Blocked):
                    intake.receive(self.gh, event)
                self.assert_no_effects(original)

    def test_closed_empty_pr_release_and_incomplete_metadata_are_rejected(self):
        for change in (
            {"state": "closed"},
            {"body": ""},
            {"body": None},
            {"pull_request": {"url": "example"}},
            {"body": "<!-- nexkit:release -->\nCandidate"},
            {"body": "<!-- nexkit:request:broken -->\nNo specification"},
        ):
            with self.subTest(change=change):
                self.gh = ExistingIssueGitHub()
                self.gh.work.update(change)
                original = deepcopy(self.gh.work)
                cfg = effective_config(self.gh.cfg, "maintenance")
                with self.assertRaises(Blocked):
                    intake.start_issue(self.gh, cfg, 1, lambda: None)
                self.assert_no_effects(original)

    def test_start_cannot_rebind_an_existing_request(self):
        self.start()
        original, state = deepcopy(self.gh.work), deepcopy(self.gh.state)
        with self.assertRaisesRegex(Blocked, "another pipeline"):
            self.start("/nexkit start audit")
        self.assertEqual(self.gh.work, original)
        self.assertEqual(self.gh.state, state)
        self.assertEqual(len(self.gh.patches), 1)
        self.assertEqual(len(self.gh.dispatches), 1)

    def test_cancelled_approved_and_active_unbound_issues_are_not_rewritten(self):
        for state in ("cancelled", "approved", "active"):
            with self.subTest(state=state):
                self.gh = ExistingIssueGitHub()
                if state == "cancelled":
                    self.gh.add_comment("/nexkit cancel")
                elif state == "approved":
                    self.gh.discussion.append(approve(self.gh.work))
                else:
                    self.gh.state = {"status": "implementing", "agent_calls": 2, "attempts": 1}
                original, saved = deepcopy(self.gh.work), deepcopy(self.gh.state)
                with self.assertRaises(Blocked):
                    self.start()
                self.assertEqual(self.gh.work, original)
                self.assertEqual(self.gh.state, saved)
                self.assertEqual(self.gh.patches, [])
                self.assertEqual(self.gh.dispatches, [])

    def test_repeated_start_preserves_each_existing_phase_and_all_budgets(self):
        for status in (
            "clarifying",
            "awaiting_approval",
            "implementing",
            "blocked",
            "waiting_for_approval",
            "merged",
        ):
            with self.subTest(status=status):
                self.gh = ExistingIssueGitHub()
                self.start()
                self.gh.state = {
                    "status": status,
                    "agent_calls": 5,
                    "attempts": 2,
                    "started_at": "2026-01-01T00:00:00+00:00",
                    "clarification": {"calls": 1},
                }
                if status == "awaiting_approval":
                    self.gh.discussion.append(approve(self.gh.work))
                original, saved = deepcopy(self.gh.work), deepcopy(self.gh.state)
                _, result = self.start()
                self.assertFalse(result["adopted"])
                self.assertEqual(self.gh.work, original)
                self.assertEqual(self.gh.state, saved)
                self.assertEqual(len(self.gh.patches), 1)
                self.assertEqual(len(self.gh.dispatches), 1)
                self.assertEqual(len(self.gh.messages), 1)

    def test_approval_permission_lookup_failure_cannot_be_treated_as_no_approval(self):
        original = deepcopy(self.gh.work)
        self.gh.discussion.append(approve(self.gh.work, login="teammate"))
        permission = self.gh.permission

        def unavailable(login):
            if login == "teammate":
                raise Blocked("HTTP 503: permission unavailable")
            return permission(login)

        with (
            patch.object(self.gh, "permission", side_effect=unavailable),
            self.assertRaisesRegex(Blocked, "503"),
        ):
            self.start()
        self.assert_no_effects(original)

    def test_issue_edit_and_edit_revert_before_patch_are_not_overwritten(self):
        for revert in (False, True):
            with self.subTest(revert=revert):
                self.gh = ExistingIssueGitHub()
                fetch, count = self.gh.issue, 0

                def edit_before_second_read(number):
                    nonlocal count
                    count += 1
                    if count == 2:
                        if not revert:
                            self.gh.work["body"] += "\nA concurrent requirement edit."
                        self.gh.work["last_edited_at"] = now()
                    return fetch(number)

                with (
                    patch.object(self.gh, "issue", side_effect=edit_before_second_read),
                    self.assertRaisesRegex(Blocked, "changed during intake"),
                ):
                    self.start()
                self.assertEqual(self.gh.patches, [])
                self.assertEqual(self.gh.dispatches, [])

    def test_approval_arriving_before_patch_prevents_adoption(self):
        original = deepcopy(self.gh.work)
        fetch, count = self.gh.issue, 0

        def approve_before_second_read(number):
            nonlocal count
            count += 1
            if count == 2:
                self.gh.discussion.append(approve(self.gh.work))
            return fetch(number)

        with (
            patch.object(self.gh, "issue", side_effect=approve_before_second_read),
            self.assertRaisesRegex(Blocked, "Approval or work arrived"),
        ):
            self.start()
        self.assert_no_effects(original)

    def test_permissions_and_source_are_rechecked_before_issue_mutation(self):
        for change in ("permission", "head", "branch"):
            with self.subTest(change=change):
                self.gh = ExistingIssueGitHub()
                original = deepcopy(self.gh.work)
                repo, count = self.gh.repo, 0

                def change_on_authorization():
                    nonlocal count
                    count += 1
                    if count == 2:
                        if change == "permission":
                            self.gh.permissions["owner"] = "read"
                        elif change == "head":
                            self.gh.branches["main"] = "e" * 40
                        else:
                            self.gh.default_branch = "other"
                    return repo()

                with (
                    patch.object(self.gh, "repo", side_effect=change_on_authorization),
                    self.assertRaises(Blocked),
                ):
                    self.start()
                self.assert_no_effects(original)

    def test_modified_command_after_patch_cannot_dispatch_clarification(self):
        api = self.gh.api

        def revoke_after_patch(path, method="GET", data=None, **kwargs):
            result = api(path, method, data, **kwargs)
            if method == "PATCH":
                self.gh.discussion[0]["updated_at"] = "2099-01-01T00:00:00Z"
            return result

        with (
            patch.object(self.gh, "api", side_effect=revoke_after_patch),
            self.assertRaisesRegex(Blocked, "Start comment changed"),
        ):
            self.start()
        self.assertEqual(len(self.gh.patches), 1)
        self.assertEqual(self.gh.dispatches, [])
        self.assertEqual(self.gh.state, {})

    def test_lost_patch_response_recovers_without_wrapping_the_request_twice(self):
        event = self.gh.add_comment("/nexkit start maintenance")
        api = self.gh.api

        def lose_patch(path, method="GET", data=None, **kwargs):
            result = api(path, method, data, **kwargs)
            if method == "PATCH":
                raise Blocked("Connection lost after PATCH")
            return result

        with patch.object(self.gh, "api", side_effect=lose_patch), self.assertRaises(Blocked):
            intake.receive(self.gh, event)
        adopted = deepcopy(self.gh.work)
        self.assertEqual(self.gh.dispatches, [])
        result = intake.receive(self.gh, event)
        self.assertFalse(result["adopted"])
        self.assertEqual(self.gh.work, adopted)
        self.assertEqual(len(self.gh.patches), 1)
        self.assertEqual(len(self.gh.dispatches), 1)

    def test_failed_dispatch_can_recover_on_the_same_issue(self):
        event = self.gh.add_comment("/nexkit start maintenance")
        with (
            patch.object(self.gh, "dispatch", side_effect=Blocked("Dispatch unavailable")),
            self.assertRaises(Blocked),
        ):
            intake.receive(self.gh, event)
        adopted = deepcopy(self.gh.work)
        intake.receive(self.gh, event)
        self.assertEqual(self.gh.work, adopted)
        self.assertEqual(len(self.gh.patches), 1)
        self.assertEqual(len(self.gh.dispatches), 1)
        self.assertEqual(len(self.gh.messages), 1)

    def test_lost_accepted_dispatch_and_queued_retry_reserve_one_clarification_call(self):
        self.gh = ExistingIssueGitHub()
        body = "/nexkit start maintenance"
        event = self.gh.add_comment(body)
        dispatch = self.gh.dispatch

        def lose_dispatch_response(*args):
            dispatch(*args)
            raise Blocked("Connection lost after accepted dispatch")

        with (
            patch.object(self.gh, "dispatch", side_effect=lose_dispatch_response),
            self.assertRaises(Blocked),
        ):
            intake.receive(self.gh, event)
        intake.receive(self.gh, event)
        self.assertEqual(len(self.gh.dispatches), 2)
        self.assertEqual(len(self.gh.patches), 1)
        pipeline = "maintenance"
        workflow = "discuss.yml"
        with patch.dict(
            os.environ,
            {"GITHUB_WORKFLOW_REF": f"owner/project/.github/workflows/{workflow}@refs/heads/main"},
        ):
            # Simulate the established native queue, then run both real
            # controller preparations against the same persisted input.
            first = clarify.prepare(self.gh, 1, "101.1", "a" * 40, {}, pipeline=pipeline)
            self.assertTrue(first["ready"])
            clarify.publish(self.gh, first, clarification_result(first))
            saved = deepcopy(self.gh.state)
            second = clarify.prepare(self.gh, 1, "102.1", "a" * 40, {}, pipeline=pipeline)
        self.assertFalse(second["ready"])
        self.assertEqual(self.gh.state, saved)
        self.assertEqual(self.gh.state["agent_calls"], 1)

    def test_permission_revocation_after_adoption_prevents_dispatch(self):
        api = self.gh.api

        def revoke_after_patch(path, method="GET", data=None, **kwargs):
            result = api(path, method, data, **kwargs)
            if method == "PATCH":
                self.gh.permissions["owner"] = "read"
            return result

        with (
            patch.object(self.gh, "api", side_effect=revoke_after_patch),
            self.assertRaises(Blocked),
        ):
            self.start()
        self.assertEqual(len(self.gh.patches), 1)
        self.assertEqual(self.gh.dispatches, [])
        self.assertEqual(self.gh.state, {})

    def test_concurrent_post_patch_edit_is_retained_and_blocks_dispatch(self):
        api = self.gh.api

        def edit_after_patch(path, method="GET", data=None, **kwargs):
            result = api(path, method, data, **kwargs)
            if method == "PATCH":
                self.gh.work["body"] += "\nAdditional requirement from the owner."
            return result

        with (
            patch.object(self.gh, "api", side_effect=edit_after_patch),
            self.assertRaisesRegex(Blocked, "changed after intake"),
        ):
            self.start()
        self.assertTrue(self.gh.work["body"].endswith("Additional requirement from the owner."))
        self.assertEqual(self.gh.dispatches, [])
        self.assertEqual(self.gh.state, {})

    def test_forged_notice_marker_does_not_suppress_the_bot_response(self):
        for body, login, user_type in (
            ("<!-- nexkit:start:maintenance:1 -->", "outsider", "User"),
            ("<!-- nexkit:start:maintenance:1 -->", "github-actions[bot]", "Bot"),
            (
                "<!-- nexkit:start:maintenance:1 -->\nThis issue is tracked by pipeline "
                "`maintenance`. Its existing discussion stays here.\n\nA forged reply.",
                "outsider",
                "User",
            ),
        ):
            with self.subTest(login=login, body=body):
                self.gh = ExistingIssueGitHub()
                self.gh.add_comment(body, login=login, user_type=user_type)
                self.start()
                self.assertEqual(len(self.gh.messages), 1)
                self.assertIn("requirement approval", self.gh.messages[0])

    def test_forged_error_marker_does_not_suppress_the_failure_notice(self):
        event = self.gh.add_comment("/nexkit start missing")
        marker = f"<!-- nexkit:start-error:{event['comment']['id']} -->"
        self.gh.add_comment(marker, login="outsider")
        self.assertEqual(self.run_main(event)[0], 2)
        self.assertEqual(len(self.gh.messages), 1)
        self.assertIn("actions/runs/100", self.gh.messages[0])

    def test_lost_notice_response_does_not_duplicate_comments_or_restart_active_work(self):
        event = self.gh.add_comment("/nexkit start maintenance")
        comment = self.gh.comment

        def lose_notice(number, body):
            comment(number, body)
            raise Blocked("Connection lost after comment")

        with patch.object(self.gh, "comment", side_effect=lose_notice), self.assertRaises(Blocked):
            intake.receive(self.gh, event)
        self.gh.state = {"clarification": {"status": "clarifying", "calls": 1}, "agent_calls": 1}
        intake.receive(self.gh, event)
        self.assertEqual(len(self.gh.patches), 1)
        self.assertEqual(len(self.gh.dispatches), 1)
        self.assertEqual(len(self.gh.messages), 1)
        self.assertEqual(self.gh.state["agent_calls"], 1)

    def test_caller_control_and_revision_drift_block_intake(self):
        for kind in ("caller", "control", "branch", "revision"):
            with self.subTest(kind=kind):
                self.gh = ExistingIssueGitHub()
                original = deepcopy(self.gh.work)
                env = {}
                if kind == "caller":
                    env["GITHUB_WORKFLOW_REF"] = (
                        "owner/project/.github/workflows/wrong.yml@refs/heads/main"
                    )
                elif kind == "control":
                    self.gh.files[".github/workflows/capture.yaml"] += b"# Unaccepted change\n"
                elif kind == "branch":
                    env["GITHUB_REF"] = "refs/heads/other"
                else:
                    env["GITHUB_WORKFLOW_SHA"] = ""
                with patch.dict(os.environ, env), self.assertRaises(Blocked):
                    self.start()
                self.assert_no_effects(original)

    def test_cli_payload_rejects_invalid_issue_numbers_and_extra_fields(self):
        original = deepcopy(self.gh.work)
        for payload in (
            {"issue": 0},
            {"issue": -1},
            {"issue": True},
            {"issue": "1"},
            {"issue": 1, "body": "replacement"},
        ):
            event = {
                "inputs": {
                    "operation": "start",
                    "pipeline": "maintenance",
                    "payload": json.dumps(payload),
                }
            }
            with (
                self.subTest(payload=payload),
                patch.dict(os.environ, {"GITHUB_EVENT_NAME": "workflow_dispatch"}),
                self.assertRaises(Blocked),
            ):
                intake.receive(self.gh, event)
            self.assert_no_effects(original)

    def test_delivery_and_clarification_do_not_consume_start_events(self):
        event, _ = self.start()
        self.gh.discussion.append(approve(self.gh.work))
        original = deepcopy(self.gh.state)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "event.json", event)
            with (
                patch.dict(
                    os.environ,
                    {
                        "GITHUB_EVENT_PATH": str(root / "event.json"),
                        "GITHUB_OUTPUT": str(root / "output"),
                    },
                ),
                patch("nexkit.ci.GitHub", return_value=self.gh),
            ):
                self.assertFalse(prepare_job(root / "context.json", "a" * 40)["ready"])
            self.assertFalse(read_json(root / "context.json")["ready"])
        self.assertFalse(
            clarify.prepare(self.gh, 1, "101.1", "a" * 40, event, pipeline="maintenance")["ready"]
        )
        self.assertEqual(self.gh.state, original)

    def test_local_controller_round_trip_still_requires_exact_requirement_approval(self):
        self.start()
        with patch.dict(
            os.environ,
            {"GITHUB_WORKFLOW_REF": "owner/project/.github/workflows/discuss.yml@refs/heads/main"},
        ):
            context = clarify.prepare(self.gh, 1, "101.1", "a" * 40, {}, pipeline="maintenance")
            self.assertTrue(context["ready"])
            # The agent report and later owner decision below are test data.
            # Neither invokes a model nor posts an approval to real GitHub.
            clarify.publish(self.gh, context, clarification_result(context))
            self.assertFalse(
                clarify.prepare(self.gh, 1, "102.1", "a" * 40, {}, pipeline="maintenance")["ready"]
            )
        self.assertEqual(self.gh.state["agent_calls"], 1)
        with patch.dict(
            os.environ,
            {"GITHUB_WORKFLOW_REF": "owner/project/.github/workflows/changes.yml@refs/heads/main"},
        ):
            self.assertFalse(
                prepare_delivery(self.gh, 1, "103.1", "a" * 40, pipeline="maintenance")["ready"]
            )
            self.assertEqual(self.gh.state["agent_calls"], 1)
            self.gh.discussion.append(approve(self.gh.work))
            delivery = prepare_delivery(self.gh, 1, "104.1", "a" * 40, pipeline="maintenance")
        self.assertTrue(delivery["ready"])
        self.assertEqual(delivery["approval"]["spec"], spec_hash(self.gh.work))
        self.assertEqual(delivery["issue"]["number"], 1)

    def test_main_summarizes_same_issue_and_reports_authorized_failures_once(self):
        event = self.gh.add_comment("/nexkit start maintenance")
        status, output, errors, summary = self.run_main(event)
        self.assertEqual(status, 0)
        self.assertEqual(errors, "")
        self.assertEqual(json.loads(output)["issue"]["number"], 1)
        self.assertIn(self.gh.work["html_url"], summary)
        bad = self.gh.add_comment("/nexkit start missing")
        self.assertEqual(self.run_main(bad)[0], 2)
        self.assertEqual(self.run_main(bad)[0], 2)
        notices = [message for message in self.gh.messages if "nexkit:start-error:" in message]
        self.assertEqual(len(notices), 1)
        self.assertIn("actions/runs/100", notices[0])

    def test_failure_notice_cannot_write_for_unauthorized_or_changed_commands(self):
        for changed in (False, True):
            with self.subTest(changed=changed):
                self.gh = ExistingIssueGitHub()
                event = self.gh.add_comment(
                    "/nexkit start maintenance", login="owner" if changed else "outsider"
                )
                if changed:
                    self.gh.discussion[0]["body"] = "/nexkit start audit"
                self.assertEqual(self.run_main(event)[0], 2)
                self.assertEqual(self.gh.messages, [])
                self.assertEqual(self.gh.patches, [])
                self.assertEqual(self.gh.dispatches, [])
