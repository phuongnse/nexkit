"""Scoped capacity decisions with a simulated native GitHub authority boundary."""

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from nexkit.budgets import limit, mark_wait, proposal, reconcile, validate
from nexkit.cli import main
from nexkit.common import Blocked, write_json
from nexkit.policy import delivery_elapsed, now, reserve
from tests.support import FakeGitHub, project_document


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        self.cfg = self.gh.cfg
        self.cfg["limits"].update(attempts=1, agent_calls=2)
        self.cfg["clarification"] = {"agent_minutes": 10}
        self.cfg = self.gh.cfg
        self.state = reserve({}, self.cfg, "100.1")

    def approve(self, extra, *, actor="owner"):
        decision = proposal(self.gh.work, self.cfg, self.state, extra)
        stamp = now()
        comment = {
            "id": 100,
            "body": decision["human_comment"],
            "created_at": stamp,
            "updated_at": stamp,
            "user": {"type": "User", "login": actor},
        }
        self.gh.discussion.append(comment)
        return comment

    def test_exhausted_work_continues_only_with_exact_extra_capacity(self):
        with self.assertRaises(Blocked):
            reserve(self.state, self.cfg, "101.1")
        self.approve({"attempts": 1, "agent_calls": 2})
        self.assertTrue(reconcile(self.gh, self.gh.work, self.cfg, self.state))
        self.assertFalse(reconcile(self.gh, self.gh.work, self.cfg, self.state))
        resumed = reserve(self.state, self.cfg, "101.1")
        self.assertEqual((resumed["attempts"], resumed["delivery_calls"]), (2, 4))
        self.assertEqual(self.cfg["limits"]["agent_calls"], 2)
        self.assertEqual(limit(resumed, self.cfg, "agent_calls"), 4)
        with self.assertRaises(Blocked):
            reserve(resumed, self.cfg, "102.1")

    def test_unauthorized_edited_and_stale_proposals_do_not_grant_capacity(self):
        original = deepcopy(self.state)
        for mutation in ("reader", "edited", "usage", "checkpoint", "spec", "config"):
            with self.subTest(mutation=mutation):
                self.state = deepcopy(original)
                self.gh.discussion = []
                comment = self.approve(
                    {"agent_calls": 2}, actor="reader" if mutation == "reader" else "owner"
                )
                work, cfg = deepcopy(self.gh.work), deepcopy(self.cfg)
                if mutation == "edited":
                    comment["updated_at"] = "changed"
                elif mutation == "usage":
                    self.state["agent_calls"] += 1
                elif mutation == "checkpoint":
                    self.state["recovery"] = {"new": "checkpoint"}
                elif mutation == "spec":
                    work["body"] += " changed"
                elif mutation == "config":
                    cfg["limits"]["minutes"] += 1
                self.assertFalse(reconcile(self.gh, work, cfg, self.state))
                self.assertNotIn("budget_grants", self.state)

    def test_deleted_revoked_or_forged_receipt_cannot_authorize_execution(self):
        self.approve({"agent_calls": 2})
        reconcile(self.gh, self.gh.work, self.cfg, self.state)
        original = deepcopy(self.state)
        for mutation in ("deleted", "revoked", "amount"):
            with self.subTest(mutation=mutation):
                state = deepcopy(original)
                if mutation == "deleted":
                    comments = self.gh.discussion
                    self.gh.discussion = []
                elif mutation == "revoked":
                    self.gh.permission = lambda _: "read"
                else:
                    state["budget_grants"][0]["additions"]["agent_calls"] = 40
                with self.assertRaises(Blocked):
                    validate(self.gh, self.gh.work, self.cfg, state)
                if mutation == "deleted":
                    self.gh.discussion = comments
                self.gh.permission = lambda login: "admin" if login == "owner" else "read"

    def test_configuration_change_retires_capacity_and_preserves_consumption(self):
        self.approve({"agent_calls": 2})
        reconcile(self.gh, self.gh.work, self.cfg, self.state)
        cfg = deepcopy(self.cfg)
        cfg["limits"]["minutes"] += 1
        self.assertFalse(reconcile(self.gh, self.gh.work, cfg, self.state))
        self.assertEqual(limit(self.state, cfg, "agent_calls"), 2)
        self.assertEqual(self.state["delivery_calls"], 2)
        self.assertEqual(len(self.state["budget_grant_history"]), 1)

    def test_edited_and_reverted_specification_expires_the_old_grant(self):
        self.approve({"agent_calls": 2})
        reconcile(self.gh, self.gh.work, self.cfg, self.state)
        work = deepcopy(self.gh.work)
        work["last_edited_at"] = "2026-10-03T09:00:00Z"
        with self.assertRaisesRegex(Blocked, "changed specification"):
            validate(self.gh, work, self.cfg, self.state)
        reconcile(self.gh, work, self.cfg, self.state)
        self.assertEqual(limit(self.state, self.cfg, "agent_calls"), 2)
        self.assertEqual(self.state["delivery_calls"], 2)

    def test_only_the_blocked_wait_interval_is_excluded_after_resume(self):
        state = reserve({}, self.cfg, "100.1", clock="2026-10-03T08:00:00+00:00")
        with patch("nexkit.budgets.now", return_value="2026-10-03T08:20:00+00:00"):
            mark_wait(state, "Agent invocation budget exhausted")
        self.state = state
        self.approve({"attempts": 1, "agent_calls": 2})
        reconcile(self.gh, self.gh.work, self.cfg, state)
        resumed = reserve(state, self.cfg, "101.1", clock="2026-10-03T09:20:00+00:00")
        self.assertEqual(resumed["budget_wait_seconds"], 3600)
        self.assertNotIn("budget_wait", resumed)
        self.assertEqual(delivery_elapsed(resumed, clock="2026-10-03T09:30:00+00:00"), 1800)
        self.assertEqual(resumed["agent_calls"], 4)

    def test_dry_run_projects_a_native_grant_with_current_remote_settings_without_writes(self):
        self.state["status"] = "blocked"
        self.approve({"attempts": 1, "agent_calls": 2})
        self.gh.state = deepcopy(self.state)
        original = deepcopy(self.gh.state)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stale = project_document(self.cfg)
            stale["defaults"]["limits"]["agent_calls"] = 40
            write_json(root / ".nexkit/project.json", stale)
            output = io.StringIO()
            with patch("nexkit.cli.GitHub", return_value=self.gh), redirect_stdout(output):
                self.assertEqual(main(["--root", str(root), "resume", "1", "--dry-run"]), 0)
            import json

            result = json.loads(output.getvalue())
            self.assertTrue(result["pending_decision_applies"])
            self.assertEqual(result["budget"]["limits"]["agent_calls"], 4)
            self.assertFalse(result["dispatch"])
        self.assertEqual(self.gh.state, original)
        self.assertEqual(self.gh.revision, 0)
        self.assertEqual(self.gh.dispatches, [])
        self.assertEqual(self.gh.messages, [])

    def test_malformed_receipt_blocks_without_an_unhandled_parser_error(self):
        self.approve({"agent_calls": 2})
        reconcile(self.gh, self.gh.work, self.cfg, self.state)
        for value in (
            {},
            {"binding": None},
            {
                **self.state["budget_grants"][0],
                "command": "/nexkit budget " + "a" * 64 + " {broken}",
            },
        ):
            with self.subTest(value=value), self.assertRaises(Blocked):
                validate(self.gh, self.gh.work, self.cfg, {"budget_grants": [value]})

    def test_invalid_or_unbounded_additions_and_completed_work_are_rejected(self):
        for extra in (
            {},
            {"agent_calls": True},
            {"minutes": -1},
            {"agent_calls": 41},
            {"call_timeout": 30},
        ):
            with self.subTest(extra=extra), self.assertRaises(Blocked):
                proposal(self.gh.work, self.cfg, self.state, extra)
        self.state["status"] = "merged"
        with self.assertRaises(Blocked):
            proposal(self.gh.work, self.cfg, self.state, {"agent_calls": 2})
