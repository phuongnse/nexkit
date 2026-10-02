import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nexkit.common import Blocked, short_summary
from nexkit.delivery import failed, finish, prepare, publish
from nexkit.github import GitHub, state_message
from tests.support import FakeGitHub, agent, approve, reviewed, verified, workflow_environment


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
        self.enterContext(patch.dict(os.environ, workflow_environment("delivery")))
        self.gh = FakeGitHub()

    def test_success_pipeline_merges_without_releasing(self):
        context = prepare(self.gh, 1, "100.1", "a" * 40, pipeline="maintenance")
        self.assertTrue(context["ready"])
        self.assertIn("Handle negative integers", self.gh.state["activity"])
        candidate = publish(self.gh, context, bundle(context))
        self.assertIn("Verifying PR", self.gh.state["activity"])
        self.assertIn("signed integer sum", self.gh.state["activity"])
        key = candidate["candidate"]
        state = finish(self.gh, candidate, verified(key), reviewed(key))
        self.assertEqual(state["status"], "merged")
        self.assertEqual(len(self.gh.merges), 1)
        self.assertEqual(self.gh.dispatches, [])
        self.assertFalse(prepare(self.gh, 1, "101.1", "a" * 40, pipeline="maintenance")["ready"])
        self.assertEqual(len(self.gh.merges), 1)

    def test_timeline_commit_includes_bounded_activity_without_changing_state(self):
        gh = GitHub("owner/project")
        state = {
            "status": "verifying",
            "activity": "Verifying PR #6:\nAdded HEAD /health support\x00",
        }
        with (
            patch.object(gh, "ensure_state_branch"),
            patch.object(gh, "api", return_value={"content": {"sha": "a" * 40}}) as api,
        ):
            gh.save_state(5, state, "b" * 40)
        data = api.call_args.args[2]
        self.assertEqual(data["message"], "NexKit #5: Verifying PR #6: Added HEAD /health support")
        self.assertEqual(data["sha"], "b" * 40)
        self.assertEqual(data["branch"], "nexkit/state")
        self.assertIn("\n", state["activity"])
        self.assertEqual(len(short_summary("a" * 1000)), 240)
        self.assertEqual(short_summary("abcdef", 2), "ab")

    def test_timeline_prioritizes_wait_block_and_merge_over_old_activity(self):
        state = {
            "activity": "Implementing an earlier change",
            "status": "waiting_for_approval",
            "pr": 6,
            "approval_wait": {"gate": "review"},
            "stage_approvals": {"review": {"definition": {"mode": "pull_request"}}},
        }
        self.assertEqual(state_message(5, state), "NexKit #5: Awaiting PR review: #6")
        state.update(status="blocked", reason="Candidate changed")
        self.assertEqual(state_message(5, state), "NexKit #5: Blocked: Candidate changed")
        state.update(status="merged")
        self.assertEqual(state_message(5, state), "NexKit #5: Merged PR #6")

    def test_failed_and_pending_clarification_progress_remain_accurate(self):
        state = {
            "clarification": {
                "status": "blocked",
                "reason": "Agent job failed",
                "activity": "Clarifying requirement: Add HEAD /health",
            }
        }
        self.assertEqual(
            state_message(5, state), "NexKit #5: Requirement blocked: Agent job failed"
        )
        for phase, label in (
            ("clarifying", "Clarifying requirement"),
            ("publishing", "Updating requirement"),
            ("awaiting_answers", "Awaiting requirement answers"),
            ("awaiting_approval", "Ready for requirement approval"),
        ):
            with self.subTest(phase=phase):
                self.assertEqual(
                    state_message(5, {"clarification": {"status": phase}}), f"NexKit #5: {label}"
                )

    def test_repair_consumes_feedback_and_rechecks_new_candidate(self):
        first = prepare(self.gh, 1, "100.1", "a" * 40, pipeline="maintenance")
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
        second = prepare(self.gh, 1, "101.1", "a" * 40, pipeline="maintenance")
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
            state = prepare(gh, 1, "100.1", "a" * 40, pipeline="maintenance")
            self.assertFalse(state["ready"])
            self.assertNotIn("agent_calls", gh.state)

    def test_cancel_before_publish_prevents_side_effect(self):
        context = prepare(self.gh, 1, "100.1", "a" * 40, pipeline="maintenance")
        cancel = approve(self.gh.work, number=11)
        cancel["body"] = "/nexkit cancel"
        self.gh.discussion.append(cancel)
        with self.assertRaises(Blocked):
            publish(self.gh, context, bundle(context))
        self.assertIsNone(self.gh.pr)

    def test_cancel_then_resume_preserves_spent_budget_and_completes_once(self):
        first = prepare(self.gh, 1, "100.1", "a" * 40, pipeline="maintenance")
        started = self.gh.state["started_at"]
        cancel = approve(self.gh.work, number=11)
        cancel["body"] = "/nexkit cancel"
        self.gh.discussion.append(cancel)
        with self.assertRaisesRegex(Blocked, "cancelled"):
            publish(self.gh, first, bundle(first))
        state = failed(self.gh, first, "Delivery cancelled")
        self.assertEqual(state["status"], "blocked")
        self.assertEqual(state["agent_calls"], 2)
        self.assertEqual(state["attempts"], 1)
        self.assertEqual(self.gh.dispatches, [])
        self.assertFalse(prepare(self.gh, 1, "101.1", "a" * 40, pipeline="maintenance")["ready"])
        self.assertIsNone(self.gh.pr)
        resume = approve(self.gh.work, number=12)
        resume["body"] = "/nexkit resume"
        self.gh.discussion.append(resume)
        second = prepare(self.gh, 1, "102.1", "a" * 40, pipeline="maintenance")
        self.assertTrue(second["ready"])
        self.assertEqual(self.gh.state["started_at"], started)
        self.assertEqual(self.gh.state["agent_calls"], 4)
        self.assertEqual(self.gh.state["attempts"], 2)
        self.assertIn("cancelled", second["feedback"]["reason"])
        candidate = publish(self.gh, second, bundle(second))
        state = finish(
            self.gh, candidate, verified(candidate["candidate"]), reviewed(candidate["candidate"])
        )
        self.assertEqual(state["status"], "merged")
        self.assertEqual(len(self.gh.merges), 1)
        self.assertFalse(prepare(self.gh, 1, "103.1", "a" * 40, pipeline="maintenance")["ready"])
        self.assertEqual(len(self.gh.merges), 1)

    def test_state_write_permission_denial_prevents_admission(self):
        before = self.gh.get_state(1)
        with (
            patch.object(
                self.gh, "save_state", side_effect=Blocked("HTTP 403: state write denied")
            ),
            self.assertRaisesRegex(Blocked, "state write denied"),
        ):
            prepare(self.gh, 1, "100.1", "a" * 40, pipeline="maintenance")
        self.assertEqual(self.gh.get_state(1), before)
        self.assertIsNone(self.gh.pr)
        self.assertEqual(self.gh.merges, [])
        self.assertEqual(self.gh.dispatches, [])
        self.assertEqual(self.gh.branches, {"main": "b" * 40})

    def test_state_read_permission_denial_is_not_an_absent_record(self):
        gh = GitHub("owner/project")
        for code in (403, 502):
            with (
                self.subTest(code=code),
                patch.object(gh, "api", side_effect=Blocked(f"HTTP {code}")),
                self.assertRaisesRegex(Blocked, f"HTTP {code}"),
            ):
                gh.get_state(1)
        with patch.object(gh, "api", side_effect=Blocked("HTTP 404")):
            self.assertEqual(gh.get_state(1), ({}, None))

    def test_pr_creation_permission_denial_never_merges_the_orphan_branch(self):
        context = prepare(self.gh, 1, "100.1", "a" * 40, pipeline="maintenance")
        real_api = self.gh.api

        def deny_pr(path, method="GET", data=None, **kwargs):
            if path.endswith("/pulls") and method == "POST":
                raise Blocked("HTTP 403: pull request creation denied")
            return real_api(path, method, data, **kwargs)

        with patch.object(self.gh, "api", side_effect=deny_pr):
            with self.assertRaisesRegex(Blocked, "pull request creation denied"):
                publish(self.gh, context, bundle(context))
            state = failed(self.gh, context, "HTTP 403: pull request creation denied")
        self.assertIsNone(self.gh.pr)
        self.assertEqual(self.gh.merges, [])
        self.assertEqual(self.gh.checks, [])
        self.assertNotEqual(state["status"], "merged")
        self.assertIn("pull request creation denied", state["reason"])
        self.assertEqual(state["agent_calls"], 2)
        self.assertEqual(state["attempts"], 1)
        self.assertIn("nexkit/maintenance/issue-1", self.gh.branches)
        orphan = self.gh.branches["nexkit/maintenance/issue-1"]
        resumed = prepare(self.gh, 1, "101.1", "a" * 40, pipeline="maintenance")
        candidate = publish(self.gh, resumed, bundle(resumed))
        self.assertEqual(candidate["candidate"]["head"], orphan)
        self.assertEqual(self.gh.commit_sequence, 1)
        state = finish(
            self.gh, candidate, verified(candidate["candidate"]), reviewed(candidate["candidate"])
        )
        self.assertEqual(state["status"], "merged")
        self.assertEqual(state["agent_calls"], 4)
        self.assertEqual(state["attempts"], 2)
        self.assertEqual(len(self.gh.merges), 1)

    def test_check_or_merge_permission_denial_cannot_report_a_merge(self):
        for operation in ("check", "merge"):
            with self.subTest(operation=operation):
                gh = FakeGitHub()
                first = prepare(gh, 1, "100.1", "a" * 40, pipeline="maintenance")
                candidate = publish(gh, first, bundle(first))
                real_api = gh.api

                def deny_merge(path, method="GET", data=None, **kwargs):
                    if path.endswith("/merge") and method == "PUT":
                        raise Blocked("HTTP 403: merge denied")
                    return real_api(path, method, data, **kwargs)

                denial = (
                    patch.object(gh, "check", side_effect=Blocked("HTTP 403: check write denied"))
                    if operation == "check"
                    else patch.object(gh, "api", side_effect=deny_merge)
                )
                with denial:
                    state = finish(
                        gh,
                        candidate,
                        verified(candidate["candidate"]),
                        reviewed(candidate["candidate"]),
                    )
                self.assertNotEqual(state["status"], "merged")
                self.assertIn("HTTP 403", state["reason"])
                self.assertEqual(state["candidate"], candidate["candidate"])
                self.assertEqual(state["agent_calls"], 2)
                self.assertEqual(state["attempts"], 1)
                self.assertEqual(gh.merges, [])
                self.assertIsNone(gh.pr["merged_at"])

    def test_spec_change_before_merge_blocks(self):
        context = prepare(self.gh, 1, "100.1", "a" * 40, pipeline="maintenance")
        context = publish(self.gh, context, bundle(context))
        self.gh.work["body"] += " Expanded scope."
        state = finish(
            self.gh, context, verified(context["candidate"]), reviewed(context["candidate"])
        )
        self.assertEqual(state["status"], "blocked")
        self.assertEqual(self.gh.merges, [])

    def test_base_drift_never_reuses_checks(self):
        context = prepare(self.gh, 1, "100.1", "a" * 40, pipeline="maintenance")
        context = publish(self.gh, context, bundle(context))
        self.gh.branches["main"] = "e" * 40
        state = finish(
            self.gh, context, verified(context["candidate"]), reviewed(context["candidate"])
        )
        self.assertEqual(self.gh.merges, [])
        self.assertIn("Base changed", state["reason"])

    def test_control_disabling_change_is_rejected(self):
        context = prepare(self.gh, 1, "100.1", "a" * 40, pipeline="maintenance")
        changes = bundle(context)
        changes["changes"][0]["path"] = ".nexkit/project.json"
        with self.assertRaises(Blocked):
            publish(self.gh, context, changes)
        self.assertIsNone(self.gh.pr)

    def test_duplicate_run_cannot_reserve_twice(self):
        prepare(self.gh, 1, "100.1", "a" * 40, pipeline="maintenance")
        count = self.gh.state["agent_calls"]
        result = prepare(self.gh, 1, "100.1", "a" * 40, pipeline="maintenance")
        self.assertFalse(result["ready"])
        self.assertEqual(self.gh.state["agent_calls"], count)

    def test_active_delivery_refuses_another_run_without_mutating_its_state(self):
        for phase in ("implementing", "verifying"):
            for run_status in ("queued", "in_progress", "waiting"):
                with self.subTest(phase=phase, run_status=run_status):
                    gh = FakeGitHub()
                    first = prepare(gh, 1, "100.1", "a" * 40, pipeline="maintenance")
                    if phase == "verifying":
                        first = publish(gh, first, bundle(first))
                    before = gh.get_state(1)
                    with patch(
                        "nexkit.delivery.run_attempt", return_value={"status": run_status}
                    ) as inspect:
                        later = prepare(gh, 1, "101.1", "a" * 40, pipeline="maintenance")
                    inspect.assert_called_once_with(gh, "100.1")
                    self.assertFalse(later["ready"])
                    self.assertIn("still active", later["reason"])
                    self.assertEqual(gh.get_state(1), before)
                    self.assertEqual(gh.dispatches, [])
                    self.assertEqual(gh.messages, [])
                    if phase == "implementing":
                        first = publish(gh, first, bundle(first))
                    state = finish(
                        gh, first, verified(first["candidate"]), reviewed(first["candidate"])
                    )
                    self.assertEqual(state["status"], "merged")
                    self.assertEqual(len(gh.merges), 1)

    def test_unavailable_prior_run_cannot_block_the_existing_delivery(self):
        for error in ("HTTP 403: Actions read permission denied", "HTTP 502: unavailable"):
            with self.subTest(error=error):
                gh = FakeGitHub()
                first = prepare(gh, 1, "100.1", "a" * 40, pipeline="maintenance")
                before = gh.get_state(1)
                with patch("nexkit.delivery.run_attempt", side_effect=Blocked(error)):
                    later = prepare(gh, 1, "101.1", "a" * 40, pipeline="maintenance")
                self.assertFalse(later["ready"])
                self.assertIn(error, later["reason"])
                self.assertEqual(gh.get_state(1), before)
                candidate = publish(gh, first, bundle(first))
                state = finish(
                    gh,
                    candidate,
                    verified(candidate["candidate"]),
                    reviewed(candidate["candidate"]),
                )
                self.assertEqual(state["status"], "merged")
                self.assertEqual(len(gh.merges), 1)

    def test_active_run_is_checked_before_another_admissions_setup(self):
        first = prepare(self.gh, 1, "100.1", "a" * 40, pipeline="maintenance")
        before = self.gh.get_state(1)
        with (
            patch("nexkit.delivery.run_attempt", return_value={"status": "in_progress"}),
            patch.object(self.gh, "repo", side_effect=Blocked("HTTP 403")) as read_setup,
        ):
            later = prepare(self.gh, 1, "101.1", "a" * 40, pipeline="maintenance")
        read_setup.assert_not_called()
        self.assertFalse(later["ready"])
        self.assertIn("still active", later["reason"])
        self.assertEqual(self.gh.get_state(1), before)
        publish(self.gh, first, bundle(first))

    def test_merge_success_then_interruption_is_recovered(self):
        context = prepare(self.gh, 1, "100.1", "a" * 40, pipeline="maintenance")
        context = publish(self.gh, context, bundle(context))
        self.gh.observe_merge()
        state = failed(self.gh, context, "Network interrupted after GitHub merged")
        self.assertEqual(state["status"], "merged")
        self.assertEqual(self.gh.merges, [])

    def test_exhausted_attempts_do_not_dispatch_again(self):
        self.gh.cfg["limits"].update(attempts=1, agent_calls=3)
        context = prepare(self.gh, 1, "100.1", "a" * 40, pipeline="maintenance")
        state = failed(self.gh, context, "Invalid CLI output")
        self.assertEqual(state["status"], "blocked")
        self.assertEqual(self.gh.dispatches, [])

    def test_separate_conversation_budget_does_not_prevent_automatic_repair(self):
        self.gh.cfg["clarification"] = {"agent_minutes": 5}
        self.gh.cfg["limits"].update(attempts=2, agent_calls=4)
        self.gh.state.update(agent_calls=12, clarification={"calls": 12})
        first = prepare(self.gh, 1, "100.1", "a" * 40, pipeline="maintenance")
        self.assertTrue(first["ready"])
        status = failed(self.gh, first, "The negative-total test failed")
        self.assertEqual(status["status"], "retry")
        self.assertEqual(len(self.gh.dispatches), 1)
        second = prepare(self.gh, 1, "101.1", "a" * 40, pipeline="maintenance")
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
        context = prepare(self.gh, 1, "100.1", "a" * 40, pipeline="maintenance")
        real_api = self.gh.api

        def interrupt(path, method="GET", data=None, **kwargs):
            if path.endswith("/pulls") and method == "POST":
                raise Blocked("Interrupted after branch creation")
            return real_api(path, method, data, **kwargs)

        self.gh.api = interrupt
        with self.assertRaises(Blocked):
            publish(self.gh, context, bundle(context))
        orphan = self.gh.branches["nexkit/maintenance/issue-1"]
        self.assertIsNone(self.gh.pr)
        self.gh.api = real_api
        resumed = prepare(self.gh, 1, "101.1", "a" * 40, pipeline="maintenance")
        self.assertEqual(resumed["source"], orphan)
        candidate = publish(self.gh, resumed, bundle(resumed))
        self.assertEqual(candidate["candidate"]["head"], orphan)
        self.assertEqual(self.gh.commit_sequence, 1)

    def test_empty_repository_can_bootstrap_with_declared_checks(self):
        self.gh.cfg["application"] = "absent"
        self.assertTrue(prepare(self.gh, 1, "100.1", "a" * 40, pipeline="maintenance")["ready"])

    def test_same_tree_still_integrates_a_new_base_before_reusing_checks(self):
        first = prepare(self.gh, 1, "100.1", "a" * 40, pipeline="maintenance")
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
        second = prepare(self.gh, 1, "101.1", "a" * 40, pipeline="maintenance")
        updated = publish(self.gh, second, bundle(second))
        head = updated["candidate"]["head"]
        self.assertNotEqual(head, old_head)
        self.assertEqual(self.gh.commits[head]["parents"], [old_head, new_base])
        self.assertEqual(self.gh.commits[head]["tree"], self.gh.commits[old_head]["tree"])

    def test_restart_after_merged_issue_was_closed_reconciles_before_approval(self):
        context = prepare(self.gh, 1, "100.1", "a" * 40, pipeline="maintenance")
        context = publish(self.gh, context, bundle(context))
        self.gh.observe_merge()
        self.gh.work["state"] = "closed"
        result = prepare(self.gh, 1, "101.1", "a" * 40, pipeline="maintenance")
        self.assertFalse(result["ready"])
        self.assertEqual(self.gh.state["status"], "merged")
        self.assertEqual(self.gh.state["agent_calls"], 2)
        self.assertEqual(self.gh.merges, [])
