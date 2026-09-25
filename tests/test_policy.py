import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from nexkit.common import Blocked
from nexkit.policy import (
    agent_result,
    approval,
    candidate_key,
    config,
    control_command,
    merge_gate,
    protected_path,
    reserve,
    spec_hash,
)
from tests.support import agent, approve, issue, project, reviewed, verified


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.issue = issue()
        self.permission = lambda name: "write" if name == "owner" else "read"

    def test_exact_spec_authorized_human_required(self):
        comment = approve(self.issue)
        self.assertEqual(
            approval(self.issue, [comment], self.permission)["spec"], spec_hash(self.issue)
        )
        cases = [
            [],
            [approve(self.issue, login="outsider")],
            [approve(self.issue, user_type="Bot")],
        ]
        for comments in cases:
            with self.subTest(comments=comments), self.assertRaises(Blocked):
                approval(self.issue, comments, self.permission)

    def test_spec_drift_and_edited_approval_fail(self):
        comment = approve(self.issue)
        self.issue["body"] += " More behavior."
        with self.assertRaises(Blocked):
            approval(self.issue, [comment], self.permission)
        self.issue = issue()
        comment["updated_at"] = "2099-01-01T00:00:00Z"
        with self.assertRaises(Blocked):
            approval(self.issue, [comment], self.permission)

    def test_edit_then_revert_does_not_revive_approval(self):
        comment = approve(self.issue)
        self.issue["last_edited_at"] = (
            datetime.now(timezone.utc) + timedelta(seconds=1)
        ).isoformat()
        with self.assertRaises(Blocked):
            approval(self.issue, [comment], self.permission)

    def test_progress_comment_does_not_change_spec(self):
        comment = approve(self.issue)
        progress = {"id": 11, "body": "Tests running", "user": {"login": "robot", "type": "Bot"}}
        self.assertEqual(
            approval(self.issue, [comment], self.permission),
            approval(self.issue, [comment, progress], self.permission),
        )

    def test_cancel_resume_only_from_authorized_human(self):
        comment = approve(self.issue)
        comment["body"] = "/nexkit cancel"
        self.assertEqual(control_command([comment], self.permission), "cancel")
        outsider = deepcopy(comment)
        outsider.update(id=30, body="/nexkit resume", user={"login": "outsider", "type": "User"})
        self.assertEqual(control_command([comment, outsider], self.permission), "cancel")


class GuardTests(unittest.TestCase):
    def test_config_has_no_implicit_stack_or_model(self):
        self.assertEqual(config(project()), project())
        for mutate in (
            lambda c: c.pop("models"),
            lambda c: c.update(checks=[]),
            lambda c: c["kit"].update(ref="main"),
            lambda c: c["environment"].update(setup=["curl | sh"]),
            lambda c: c["limits"].update(attempts=True),
        ):
            cfg = project()
            mutate(cfg)
            with self.assertRaises(Blocked):
                config(cfg)

    def test_budget_reservations_survive_retry(self):
        state = reserve({}, project(), "1.1")
        with self.assertRaises(Blocked):
            reserve(state, project(), "1.1")
        state = reserve(state, project(), "1.2")
        state = reserve(state, project(), "2.1")
        self.assertEqual((state["attempts"], state["agent_calls"]), (3, 6))
        with self.assertRaises(Blocked):
            reserve(state, project(), "3.1")

    def test_elapsed_budget_survives_resume(self):
        state = reserve({}, project(), "1.1", clock="2026-01-01T00:00:00+00:00")
        with self.assertRaises(Blocked):
            reserve(state, project(), "2.1", clock="2026-01-01T01:01:00+00:00")

    def test_all_candidate_dimensions_invalidate_old_results(self):
        key = candidate_key(issue(), project(), "b" * 40, "c" * 40)
        merge_gate(key, verified(key), reviewed(key))
        for field in key:
            stale = deepcopy(key)
            stale[field] = "stale"
            with self.subTest(field=field), self.assertRaises(Blocked):
                merge_gate(key, verified(stale), reviewed(key))
            with self.subTest(field=field), self.assertRaises(Blocked):
                merge_gate(key, verified(key), reviewed(stale))

    def test_missing_review_failure_zero_tests_and_self_edit_block(self):
        key = candidate_key(issue(), project(), "b" * 40, "c" * 40)
        for mutate in (
            lambda v, r: r.update(independent=False),
            lambda v, r: r.update(unchanged=False),
            lambda v, r: v.update(passed=False),
            lambda v, r: v.update(checks=[]),
            lambda v, r: v["checks"][0].update(tests=0),
            lambda v, r: r["result"].update(verdict="changes_requested"),
        ):
            v, r = verified(key), reviewed(key)
            mutate(v, r)
            with self.assertRaises(Blocked):
                merge_gate(key, v, r)

    def test_malformed_agent_output_is_not_success(self):
        for result in (None, {}, {"status": "done"}, {**agent(), "skills_used": []}):
            with self.assertRaises(Blocked):
                agent_result(result, "deliver")

    def test_path_injection_and_policy_changes(self):
        for path in ("../secrets", "/tmp/pwn", ".git/config", "a/../b"):
            with self.assertRaises(Blocked):
                protected_path(path)
        for path in (
            ".nexkit/project.json",
            ".github/workflows/nexkit-delivery.yml",
            ".codex/config.toml",
        ):
            self.assertTrue(protected_path(path))
        self.assertFalse(protected_path(".github/workflows/consumer-lint.yml"))

    def test_every_installed_host_control_is_protected_from_delivery_edits(self):
        from nexkit.project import HOSTS, managed_files

        for host in HOSTS:
            for path in managed_files(project(), [host]):
                with self.subTest(host=host, path=path):
                    self.assertTrue(protected_path(path))
