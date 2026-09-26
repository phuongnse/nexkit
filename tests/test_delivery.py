import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nexkit.common import Blocked
from nexkit.delivery import failed, finish, prepare, publish
from tests.support import FakeGitHub, agent, approve, reviewed, verified


def bundle(context, content="print(sum(map(int, args)))\n"):
    return {
        "run_key": context["run_key"],
        "source": context["source"],
        "base": context["base"],
        "result": agent(),
        "changes": [{"path": "cli.py", "mode": "100644", "content": content}],
    }


class DeliveryTests(unittest.TestCase):
    """Exercises controller decisions with a labeled fake GitHub boundary."""

    def setUp(self):
        self.gh = FakeGitHub()

    def test_success_pipeline_merges_without_releasing(self):
        context = prepare(self.gh, 1, "100.1", "a" * 40)
        self.assertTrue(context["ready"])
        candidate = publish(self.gh, context, bundle(context))
        key = candidate["candidate"]
        state = finish(self.gh, candidate, verified(key), reviewed(key))
        self.assertEqual(state["status"], "merged")
        self.assertEqual(len(self.gh.merges), 1)
        self.assertEqual(self.gh.dispatches, [])
        self.assertFalse(prepare(self.gh, 1, "101.1", "a" * 40)["ready"])
        self.assertEqual(len(self.gh.merges), 1)

    def test_repair_consumes_feedback_and_rechecks_new_candidate(self):
        first = prepare(self.gh, 1, "100.1", "a" * 40)
        initial = publish(self.gh, first, bundle(first, "print(abs(sum(args)))\n"))
        review = reviewed(initial["candidate"])
        review["result"].update(
            verdict="changes_requested",
            findings=[
                {
                    "severity": "blocking",
                    "detail": "Negative totals are incorrectly changed to positive",
                }
            ],
        )
        status = finish(self.gh, initial, verified(initial["candidate"]), review)
        self.assertEqual(status["status"], "retry")
        self.assertEqual(self.gh.merges, [])
        second = prepare(self.gh, 1, "101.1", "a" * 40)
        self.assertIn("Negative totals", str(second["feedback"]))
        fixed = publish(self.gh, second, bundle(second))
        self.assertNotEqual(initial["candidate"], fixed["candidate"])
        state = finish(self.gh, fixed, verified(fixed["candidate"]), reviewed(fixed["candidate"]))
        self.assertEqual(state["status"], "merged")
        self.assertEqual(state["agent_calls"], 4)

    def test_unauthorized_and_missing_approval_do_not_start_agent(self):
        for comments in (
            [],
            [approve(self.gh.work, login="outsider")],
            [approve(self.gh.work, user_type="Bot")],
        ):
            gh = FakeGitHub()
            gh.discussion = comments
            state = prepare(gh, 1, "100.1", "a" * 40)
            self.assertFalse(state["ready"])
            self.assertNotIn("agent_calls", gh.state)

    def test_cancel_before_publish_prevents_side_effect(self):
        context = prepare(self.gh, 1, "100.1", "a" * 40)
        cancel = approve(self.gh.work, number=11)
        cancel["body"] = "/nexkit cancel"
        self.gh.discussion.append(cancel)
        with self.assertRaises(Blocked):
            publish(self.gh, context, bundle(context))
        self.assertIsNone(self.gh.pr)

    def test_spec_change_before_merge_blocks(self):
        context = prepare(self.gh, 1, "100.1", "a" * 40)
        context = publish(self.gh, context, bundle(context))
        self.gh.work["body"] += " Expanded scope."
        state = finish(
            self.gh, context, verified(context["candidate"]), reviewed(context["candidate"])
        )
        self.assertEqual(state["status"], "blocked")
        self.assertEqual(self.gh.merges, [])

    def test_base_drift_never_reuses_checks(self):
        context = prepare(self.gh, 1, "100.1", "a" * 40)
        context = publish(self.gh, context, bundle(context))
        self.gh.branches["main"] = "e" * 40
        state = finish(
            self.gh, context, verified(context["candidate"]), reviewed(context["candidate"])
        )
        self.assertEqual(self.gh.merges, [])
        self.assertIn("Base changed", state["reason"])

    def test_control_disabling_change_is_rejected(self):
        context = prepare(self.gh, 1, "100.1", "a" * 40)
        changes = bundle(context)
        changes["changes"][0]["path"] = ".nexkit/project.json"
        with self.assertRaises(Blocked):
            publish(self.gh, context, changes)
        self.assertIsNone(self.gh.pr)

    def test_duplicate_run_cannot_reserve_twice(self):
        prepare(self.gh, 1, "100.1", "a" * 40)
        count = self.gh.state["agent_calls"]
        result = prepare(self.gh, 1, "100.1", "a" * 40)
        self.assertFalse(result["ready"])
        self.assertEqual(self.gh.state["agent_calls"], count)

    def test_merge_success_then_interruption_is_recovered(self):
        context = prepare(self.gh, 1, "100.1", "a" * 40)
        context = publish(self.gh, context, bundle(context))
        self.gh.pr.update(merged_at="2026-01-01T00:00:00Z", merge_commit_sha="d" * 40)
        state = failed(self.gh, context, "Network interrupted after GitHub merged")
        self.assertEqual(state["status"], "merged")
        self.assertEqual(self.gh.merges, [])

    def test_exhausted_attempts_do_not_dispatch_again(self):
        self.gh.cfg["limits"].update(attempts=1, agent_calls=3)
        context = prepare(self.gh, 1, "100.1", "a" * 40)
        state = failed(self.gh, context, "Invalid CLI output")
        self.assertEqual(state["status"], "blocked")
        self.assertEqual(self.gh.dispatches, [])

    def test_separate_conversation_budget_does_not_prevent_automatic_repair(self):
        self.gh.cfg["clarification"] = {"agent_minutes": 5}
        self.gh.cfg["limits"].update(attempts=2, agent_calls=4)
        self.gh.state.update(agent_calls=12, clarification={"calls": 12})
        first = prepare(self.gh, 1, "100.1", "a" * 40)
        self.assertTrue(first["ready"])
        status = failed(self.gh, first, "The negative-total test failed")
        self.assertEqual(status["status"], "retry")
        self.assertEqual(len(self.gh.dispatches), 1)
        second = prepare(self.gh, 1, "101.1", "a" * 40)
        self.assertTrue(second["ready"])
        self.assertIn("negative-total", second["feedback"]["reason"])
        self.assertEqual(self.gh.state["delivery_calls"], 4)
        self.assertEqual(self.gh.state["agent_calls"], 16)
        self.assertEqual(failed(self.gh, second, "Still failing")["status"], "blocked")
        self.assertEqual(len(self.gh.dispatches), 1)

    def test_outsider_event_cannot_spend_an_existing_approval_budget(self):
        from nexkit.ci import prepare_job

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            event = {"issue": self.gh.work, "sender": {"login": "outsider", "type": "User"}}
            (path / "event.json").write_text(json.dumps(event))
            env = {
                "GITHUB_REPOSITORY": self.gh.repository,
                "GITHUB_REF": "refs/heads/main",
                "GITHUB_EVENT_NAME": "issue_comment",
                "GITHUB_EVENT_PATH": str(path / "event.json"),
                "GITHUB_OUTPUT": str(path / "output"),
            }
            with patch.dict(os.environ, env), patch("nexkit.ci.GitHub", return_value=self.gh):
                result = prepare_job(path / "context.json", "a" * 40)
            self.assertFalse(result["ready"])
            self.assertNotIn("agent_calls", self.gh.state)

    def test_internal_dispatch_remains_an_authorized_continuation(self):
        from nexkit.ci import authorized_event

        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "workflow_dispatch"}):
            self.assertTrue(authorized_event(self.gh))

    def test_orphan_branch_after_interruption_is_recovered_without_empty_commit(self):
        context = prepare(self.gh, 1, "100.1", "a" * 40)
        real_api = self.gh.api

        def interrupt(path, method="GET", data=None, **kwargs):
            if path.endswith("/pulls") and method == "POST":
                raise Blocked("Interrupted after branch creation")
            return real_api(path, method, data, **kwargs)

        self.gh.api = interrupt
        with self.assertRaises(Blocked):
            publish(self.gh, context, bundle(context))
        orphan = self.gh.branches["nexkit/issue-1"]
        self.assertIsNone(self.gh.pr)
        self.gh.api = real_api
        resumed = prepare(self.gh, 1, "101.1", "a" * 40)
        self.assertEqual(resumed["source"], orphan)
        candidate = publish(self.gh, resumed, bundle(resumed))
        self.assertEqual(candidate["candidate"]["head"], orphan)
        self.assertEqual(self.gh.commit_sequence, 1)

    def test_empty_repository_can_bootstrap_with_declared_checks(self):
        self.gh.cfg["application"] = "absent"
        self.assertTrue(prepare(self.gh, 1, "100.1", "a" * 40)["ready"])

    def test_same_tree_still_integrates_a_new_base_before_reusing_checks(self):
        first = prepare(self.gh, 1, "100.1", "a" * 40)
        candidate = publish(self.gh, first, bundle(first))
        old_head = candidate["candidate"]["head"]
        old_base = first["base"]
        new_base = "e" * 40
        self.gh.commits[new_base] = {
            "sha": new_base,
            "tree": self.gh.commits[old_base]["tree"],
            "parents": [old_base],
        }
        self.gh.branches["main"] = new_base
        second = prepare(self.gh, 1, "101.1", "a" * 40)
        updated = publish(self.gh, second, bundle(second))
        head = updated["candidate"]["head"]
        self.assertNotEqual(head, old_head)
        self.assertEqual(self.gh.commits[head]["parents"], [old_head, new_base])
        self.assertEqual(self.gh.commits[head]["tree"], self.gh.commits[old_head]["tree"])

    def test_restart_after_merged_issue_was_closed_reconciles_before_approval(self):
        context = prepare(self.gh, 1, "100.1", "a" * 40)
        context = publish(self.gh, context, bundle(context))
        self.gh.pr.update(merged_at="2026-01-01T00:00:00Z", merge_commit_sha="d" * 40)
        self.gh.work["state"] = "closed"
        result = prepare(self.gh, 1, "101.1", "a" * 40)
        self.assertFalse(result["ready"])
        self.assertEqual(self.gh.state["status"], "merged")
        self.assertEqual(self.gh.state["agent_calls"], 2)
        self.assertEqual(self.gh.merges, [])
