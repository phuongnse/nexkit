"""Manual publication uses simulated GitHub; no live approval is supplied."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from nexkit import clarify, cli, intake
from nexkit.common import Blocked, write_json
from nexkit.pipelines import effective_config
from nexkit.policy import spec_hash
from tests.support import approve
from tests.test_clarify import RequirementGitHub
from tests.test_issue_start import ExistingIssueGitHub


class ManualSpecificationTests(unittest.TestCase):
    def setUp(self):
        self.gh = ExistingIssueGitHub()
        self.gh.work["body"] = RequirementGitHub().work["body"]
        self.specification = "## Goal\n\nSum signed integers.\n"
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "GITHUB_REPOSITORY": self.gh.repository,
                    "GITHUB_ACTOR": "owner",
                    "GITHUB_EVENT_NAME": "workflow_dispatch",
                    "GITHUB_REF": "refs/heads/main",
                    "GITHUB_WORKFLOW_REF": "owner/project/.github/workflows/capture.yaml@refs/heads/main",
                    "GITHUB_WORKFLOW_SHA": "b" * 40,
                    "NEXKIT_PIPELINE": "",
                },
            )
        )

    def payload(self):
        return cli.specification_payload(self.gh.work, self.specification)

    def publish(self, payload=None, authorize=lambda: None):
        return intake.publish_manual_spec(
            self.gh,
            effective_config(self.gh.cfg, "maintenance"),
            payload or self.payload(),
            authorize,
        )

    def receive(self, payload=None, pipeline="maintenance"):
        return intake.receive(
            self.gh,
            {
                "inputs": {
                    "operation": "spec",
                    "pipeline": pipeline,
                    "payload": json.dumps(payload or self.payload()),
                }
            },
        )

    def test_original_request_is_collapsed_and_specification_remains_visible(self):
        original = "## Request\n\nKeep `$(literal)` and -2."
        body = cli.request_body(original, "example", pipeline="maintenance")
        preserved = body.split("<summary>Original request</summary>\n\n", 1)[1].split(
            "\n\n</details>", 1
        )[0]
        self.assertEqual(
            "\n".join(line.removeprefix("> ") for line in preserved.splitlines()), original
        )
        self.assertLess(body.index("</details>"), body.index(cli.SPEC_MARKER))
        self.assertTrue(body.startswith("<!-- nexkit:request:maintenance:example -->\n"))

    def test_publish_posts_the_exact_current_approval_command_without_an_agent(self):
        result = self.publish()
        self.assertEqual(result["spec"], spec_hash(self.gh.work))
        self.assertIn("> Signed sum", self.gh.work["body"])
        self.assertTrue(self.gh.work["body"].endswith(self.specification))
        self.assertIn("/nexkit approve " + result["spec"], self.gh.messages[0])
        self.assertIn("Read the specification", self.gh.messages[0])
        self.assertNotIn("human", self.gh.messages[0])
        self.assertEqual(self.gh.state, {})
        self.assertEqual(self.gh.dispatches, [])

    def test_repeat_does_not_edit_the_issue_or_duplicate_the_notice(self):
        expected = self.publish()
        edited = self.gh.work["last_edited_at"]
        with patch.object(self.gh, "api", wraps=self.gh.api) as api:
            self.assertEqual(self.publish(), expected)
        api.assert_not_called()
        self.assertEqual(self.gh.work["last_edited_at"], edited)
        self.assertEqual(len(self.gh.messages), 1)

    def test_changed_specification_gets_its_own_notice(self):
        previous = self.publish()
        self.specification += "\nNo arguments print zero.\n"
        current = self.publish()
        self.assertNotEqual(previous["spec"], current["spec"])
        self.assertEqual(len(self.gh.messages), 2)
        self.assertIn(current["spec"], self.gh.messages[-1])

    def test_manual_notice_from_cli_account_cannot_start_a_model_or_become_an_answer(self):
        event = self.gh.add_comment(cli.MANUAL_SPEC_NOTICE + "a" * 64 + " -->\nReady for review.")
        self.assertFalse(
            clarify.prepare(self.gh, 1, "100.1", "a" * 40, event, pipeline="maintenance")["ready"]
        )
        self.assertEqual(clarify.discussion(self.gh, 1), [])
        self.assertEqual(self.gh.state, {})
        self.gh.add_comment("Does the CLI handle negative inputs?")
        self.assertEqual(len(clarify.discussion(self.gh, 1)), 1)

    def test_lost_patch_response_recovers_without_another_edit(self):
        original = self.gh.api
        payload = self.payload()

        def lose_response(*args, **kwargs):
            original(*args, **kwargs)
            raise Blocked("Response lost")

        with patch.object(self.gh, "api", side_effect=lose_response):
            with self.assertRaisesRegex(Blocked, "Response lost"):
                self.publish(payload)
        with patch.object(self.gh, "api", wraps=original) as api:
            self.publish(payload)
        api.assert_not_called()
        self.assertEqual(len(self.gh.messages), 1)

    def test_lost_comment_response_does_not_duplicate_the_notice(self):
        original = self.gh.comment
        payload = self.payload()

        def lose_response(*args):
            original(*args)
            raise Blocked("Response lost")

        with patch.object(self.gh, "comment", side_effect=lose_response):
            with self.assertRaisesRegex(Blocked, "Response lost"):
                self.publish(payload)
        self.publish(payload)
        self.assertEqual(len(self.gh.messages), 1)

    def test_concurrent_edit_prevents_a_stale_notice(self):
        original = self.gh.issue
        reads = 0

        def changed(number):
            nonlocal reads
            reads += 1
            if reads == 3:
                self.gh.work["body"] += "\nChanged concurrently.\n"
            return original(number)

        with patch.object(self.gh, "issue", side_effect=changed):
            with self.assertRaisesRegex(Blocked, "changed before its approval notice"):
                self.publish()
        self.assertEqual(self.gh.messages, [])

    def test_closed_issue_cannot_receive_a_new_specification_or_notice(self):
        self.gh.work["state"] = "closed"
        before = self.gh.work["body"]
        with self.assertRaisesRegex(Blocked, "Work item is closed"):
            self.publish()
        self.assertEqual(self.gh.work["body"], before)
        self.assertEqual(self.gh.messages, [])

    def test_issue_closed_between_reads_is_rechecked_before_editing(self):
        original = self.gh.issue
        before = self.gh.work["body"]
        reads = 0

        def closed(number):
            nonlocal reads
            reads += 1
            if reads == 2:
                self.gh.work["state"] = "closed"
            return original(number)

        with patch.object(self.gh, "issue", side_effect=closed):
            with self.assertRaisesRegex(Blocked, "Work item is closed"):
                self.publish()
        self.assertEqual(self.gh.work["body"], before)
        self.assertEqual(self.gh.messages, [])

    def test_cli_spec_only_queues_actions_without_a_personal_issue_edit_or_comment(self):
        before = deepcopy(self.gh.work)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / ".nexkit/project.json", self.gh.cfg)
            source = root / "requirement.md"
            source.write_text(self.specification)
            with (
                patch("nexkit.cli.GitHub", return_value=self.gh),
                redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(
                    cli.main(["--root", str(root), "spec", "1", "--body-file", str(source)]), 0
                )
        result = json.loads(output.getvalue())
        self.assertTrue(result["queued"])
        self.assertIn("github-actions[bot]", result["note"])
        self.assertEqual(result["lookup"], "nexkit --pipeline maintenance status 1")
        self.assertNotIn("human_approval_comment", result)
        self.assertEqual(self.gh.work, before)
        self.assertEqual(self.gh.patches, [])
        self.assertEqual(self.gh.messages, [])
        workflow, branch, inputs = self.gh.dispatches.pop()
        self.assertEqual((workflow, branch, inputs["operation"]), ("capture.yaml", "main", "spec"))
        self.assertEqual(json.loads(inputs["payload"]), self.payload())
        published = intake.receive(self.gh, {"inputs": inputs})
        self.assertEqual(published["spec"], spec_hash(self.gh.work))
        self.assertEqual(self.gh.discussion[-1]["user"]["login"], "github-actions[bot]")
        self.assertEqual(self.gh.dispatches, [])
        self.assertEqual(self.gh.state, {})

    def test_existing_user_notice_cannot_suppress_the_actions_notice(self):
        self.publish()
        self.gh.discussion[-1]["user"] = {"login": "owner", "type": "User"}
        before = deepcopy(self.gh.work)
        self.receive()
        self.receive()
        self.assertEqual(self.gh.work, before)
        self.assertEqual(len(self.gh.messages), 2)
        self.assertEqual(self.gh.discussion[-1]["user"]["login"], "github-actions[bot]")

    def test_another_bot_cannot_suppress_the_actions_notice(self):
        self.publish()
        self.gh.discussion[-1]["user"]["login"] = "another[bot]"
        self.receive()
        self.assertEqual(len(self.gh.messages), 2)

    def test_native_repeat_preserves_approval_usage_and_the_issue(self):
        payload = self.payload()
        self.receive(payload)
        self.gh.discussion.append(approve(self.gh.work))
        self.gh.state = {
            "agent_calls": 1,
            "clarification": {"calls": 1, "status": "awaiting_answers"},
        }
        issue, discussion, state = map(deepcopy, (self.gh.work, self.gh.discussion, self.gh.state))
        self.receive(payload)
        self.assertEqual(
            (self.gh.work, self.gh.discussion, self.gh.state), (issue, discussion, state)
        )
        self.assertEqual(self.gh.dispatches, [])

    def test_queued_body_or_title_change_requires_a_new_submission(self):
        for field in ("body", "title", "last_edited_at"):
            with self.subTest(field=field):
                before = deepcopy(self.gh.work)
                payload = self.payload()
                self.gh.work[field] = (self.gh.work[field] or "") + " changed"
                changed = deepcopy(self.gh.work)
                with self.assertRaisesRegex(Blocked, "changed while publication was queued"):
                    self.receive(payload)
                self.assertEqual(self.gh.work, changed)
                self.gh.work = before
        self.assertEqual(self.gh.messages, [])
        self.assertEqual(self.gh.patches, [])

    def test_native_publication_rejects_a_different_pipeline(self):
        before = deepcopy(self.gh.work)
        with self.assertRaisesRegex(Blocked, "another pipeline"):
            self.receive(pipeline="audit")
        self.assertEqual(self.gh.work, before)
        self.assertEqual(self.gh.messages, [])

    def test_unauthorized_dispatch_cannot_edit_or_comment(self):
        self.gh.permissions["owner"] = "read"
        before = deepcopy(self.gh.work)
        with self.assertRaisesRegex(Blocked, "authorized project collaborator"):
            self.receive()
        self.assertEqual(self.gh.work, before)
        self.assertEqual(self.gh.messages, [])

    def test_permission_is_rechecked_immediately_before_the_write(self):
        before = deepcopy(self.gh.work)
        with patch.object(self.gh, "permission", side_effect=["admin", "read"]):
            with self.assertRaisesRegex(Blocked, "permission was revoked"):
                self.receive()
        self.assertEqual(self.gh.work, before)
        self.assertEqual(self.gh.messages, [])

    def test_authorization_failure_after_patch_recovers_only_the_missing_notice(self):
        payload = self.payload()
        with patch.object(self.gh, "permission", side_effect=["admin", "admin", "read"]):
            with self.assertRaisesRegex(Blocked, "permission was revoked"):
                self.receive(payload)
        self.assertEqual(len(self.gh.patches), 1)
        self.assertEqual(self.gh.messages, [])
        self.receive(payload)
        self.assertEqual(len(self.gh.patches), 1)
        self.assertEqual(len(self.gh.messages), 1)

    def test_wrong_caller_and_changed_accepted_files_cannot_publish(self):
        before = deepcopy(self.gh.work)
        with patch.dict(os.environ, {"GITHUB_WORKFLOW_REF": "wrong/workflow"}):
            with self.assertRaisesRegex(Blocked, "configured entrypoint"):
                self.receive()
        self.gh.files[".github/workflows/capture.yaml"] += b"# changed\n"
        with self.assertRaisesRegex(Blocked, "Accepted workflow/control file changed"):
            self.receive()
        self.assertEqual(self.gh.work, before)
        self.assertEqual(self.gh.messages, [])

    def test_invalid_submission_fields_and_size_do_not_write(self):
        payload = self.payload()
        invalid = [
            {**payload, "issue": True},
            {**payload, "issue": 0},
            {**payload, "body": " "},
            {**payload, "body": 123},
            {**payload, "expected": "invalid"},
            {**payload, "target": "invalid"},
            {**payload, "edited_at": 123},
            {**payload, "extra": "unexpected"},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(Blocked):
                self.receive(value)
        with self.assertRaisesRegex(Blocked, "issue size bound"):
            cli.specification_payload(self.gh.work, "x" * 60000)
        with self.assertRaisesRegex(Blocked, "50 KB"):
            cli.submit(
                self.gh,
                effective_config(self.gh.cfg, "maintenance"),
                "spec",
                {**payload, "body": "x" * 50000},
            )
        self.assertEqual(self.gh.patches, [])
        self.assertEqual(self.gh.messages, [])
        self.assertEqual(self.gh.dispatches, [])


if __name__ == "__main__":
    unittest.main()
