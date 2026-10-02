"""Human checkpoints with mocked GitHub boundaries; no human decisions fabricated live."""

import hashlib
import io
import os
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import yaml

from nexkit import approvals, invocations
from nexkit.checks import combine_checks
from nexkit.ci import main, prepare_check
from nexkit.common import Blocked, canonical, read_json, write_json
from nexkit.delivery import failed, finish, prepare, publish, revalidate
from nexkit.github import state_message
from nexkit.pipelines import effective_config
from nexkit.policy import config, now
from tests.test_invocations import InvocationGitHub, check_reports, report


class ApprovalGitHub(InvocationGitHub):
    def __init__(self, mode="issue", *, subject="stage", protects=None):
        super().__init__()
        self.reviews = []
        self.inline = []
        self.run_status = "completed"
        self.attempt_statuses = {}
        self.run_queries = []
        self.review_policy = []
        path = ".github/workflows/continue.yml"
        self.files[path] = b"name: Accepted continuation\non: workflow_dispatch\njobs: {}\n"
        self.cfg["files"][path] = {
            "managed": True,
            "sha256": hashlib.sha256(self.files[path]).hexdigest(),
        }
        self.cfg["pipelines"]["maintenance"]["approvals"] = {
            "owner-check": {
                "enabled": True,
                "mode": mode,
                "subject": subject,
                "reviewers": ["owner"],
                "minimum": 1,
                "wait_minutes": 1440,
                "on_rejection": "retry",
                "continuation": path,
                "protects": protects or ["invocation:edit"],
            }
        }

    def permission(self, login):
        return "admin" if login in ("owner", "second") else "read"

    def api(self, path, method="GET", data=None, **kwargs):
        if "/reviews/" in path and "/comments" in path:
            return deepcopy(self.inline)
        if path.endswith("/reviews?per_page=100"):
            return deepcopy(self.reviews)
        if "/actions/runs/" in path:
            self.run_queries.append(path)
            return {"status": self.attempt_statuses.get(path, self.run_status)}
        return super().api(path, method, data, **kwargs)

    def comment(self, number, body):
        super().comment(number, body)
        self.discussion.append(
            {
                "id": 1000 + len(self.messages),
                "body": body,
                "created_at": now(),
                "updated_at": now(),
                "user": {"login": "github-actions[bot]", "type": "Bot"},
            }
        )


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.gh = ApprovalGitHub()
        self.env = patch.dict(
            os.environ,
            {
                "GITHUB_WORKFLOW_REF": "owner/project/.github/workflows/changes.yml@refs/heads/main",
                "GITHUB_WORKFLOW_SHA": "b" * 40,
                "GITHUB_REPOSITORY": "owner/project",
                "GITHUB_RUN_ID": "100",
                "GITHUB_RUN_ATTEMPT": "1",
                "GITHUB_REF": "refs/heads/main",
                "GITHUB_EVENT_NAME": "workflow_dispatch",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.context = self.start()

    def start(self, key="100.1"):
        result = prepare(self.gh, 1, key, "a" * 40, pipeline="maintenance", individual_agents=True)
        self.assertTrue(result["ready"], result)
        return result

    def request(self, context=None, **kwargs):
        return approvals.request(self.gh, context or self.context, "owner-check", **kwargs)

    def command(self, *, changes=None, login="owner", number=2000):
        record = self.gh.state["stage_approvals"]["owner-check"]
        verb = "request-changes" if changes else "approve-stage"
        stamp = now()
        comment = {
            "id": number,
            "body": f"/nexkit {verb} owner-check {record['digest']}"
            + ("\n" + changes if changes else ""),
            "created_at": stamp,
            "updated_at": stamp,
            "user": {"login": login, "type": "User"},
        }
        self.gh.discussion.append(comment)
        return comment

    def resume(self, key="200.1"):
        os.environ["GITHUB_WORKFLOW_REF"] = (
            "owner/project/.github/workflows/continue.yml@refs/heads/main"
        )
        os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"] = key.split(".")
        return approvals.resume(self.gh, 1, "maintenance", "owner-check", "a" * 40, key)

    def candidate(self):
        editor = invocations.prepare(self.gh, self.context, "edit")
        output = report(editor)
        invocations.record(self.gh, editor, output)
        return publish(self.gh, editor, output)

    def pr_checkpoint(self, *, reviewers=None, minimum=1):
        os.environ["GITHUB_WORKFLOW_REF"] = (
            "owner/project/.github/workflows/changes.yml@refs/heads/main"
        )
        os.environ["GITHUB_RUN_ID"] = "100"
        self.gh = ApprovalGitHub("pull_request", subject="candidate", protects=["merge"])
        definition = self.gh.cfg["pipelines"]["maintenance"]["approvals"]["owner-check"]
        definition.update(reviewers=["owner"] if reviewers is None else reviewers, minimum=minimum)
        self.context = self.start()
        candidate = self.candidate()
        checks = check_reports(candidate)
        reviewer = invocations.prepare(self.gh, candidate, "audit", check_reports=checks)
        reviewed = report(reviewer)
        invocations.record(self.gh, reviewer, reviewed)
        self.request(candidate, checks=checks, review=reviewed)
        return candidate, checks, reviewed

    def review(self, candidate, *, status="APPROVED", login="owner", number=20):
        value = {
            "id": number,
            "state": status,
            "body": "Check the boundary case.",
            "submitted_at": now(),
            "commit_id": candidate["candidate"]["head"],
            "user": {"login": login, "type": "User"},
        }
        self.gh.reviews.append(value)
        return value

    def test_disabled_gate_does_not_pause_or_require_a_human(self):
        self.gh.cfg["pipelines"]["maintenance"]["approvals"]["owner-check"]["enabled"] = False
        self.context = self.start("101.1")
        result = self.request()
        self.assertTrue(result["ready"])
        self.assertNotIn("approval_wait", self.gh.state)
        invocations.prepare(self.gh, self.context, "edit")
        self.assertEqual(self.gh.state["agent_calls"], 1)

    def test_config_rejects_unknown_controls_and_unbound_capabilities(self):
        original = deepcopy(self.gh.cfg)
        for field, bad in (
            ("minimum", 2),
            ("wait_minutes", 0),
            ("enabled", "yes"),
            ("reviewers", ["owner", "OWNER"]),
            ("reviewers", "anyone"),
            ("reviewers", []),
            ("reviewers", None),
            ("reviewers", {"permission": "write"}),
            ("protects", ["invocation:missing"]),
            ("continuation", ".github/workflows/unaccepted.yml"),
        ):
            with self.subTest(field=field):
                value = deepcopy(original)
                value["pipelines"]["maintenance"]["approvals"]["owner-check"][field] = bad
                with self.assertRaises(Blocked):
                    config(value)
        self.assertEqual(approvals.pr_review_count(effective_config(original, "maintenance")), 0)

    def test_repository_reviewers_require_positive_integer_quorum(self):
        definition = self.gh.cfg["pipelines"]["maintenance"]["approvals"]["owner-check"]
        definition["reviewers"] = "repository"
        for minimum in (0, -1, True, 1.5, "2"):
            with self.subTest(minimum=minimum):
                definition["minimum"] = minimum
                with self.assertRaises(Blocked):
                    config(self.gh.cfg)
        definition["minimum"] = 2
        config(self.gh.cfg)

    def test_repository_issue_approval_accepts_distinct_current_collaborators(self):
        definition = self.gh.cfg["pipelines"]["maintenance"]["approvals"]["owner-check"]
        definition.update(reviewers="repository", minimum=2)
        self.context = self.start("101.1")
        self.request()
        self.command(login="second")
        self.command(login="second", number=2001)
        self.assertFalse(self.resume()["ready"])
        self.command(number=2002)
        resumed = self.resume()["context"]
        with patch.object(
            self.gh, "permission", side_effect=lambda login: "admin" if login == "owner" else "read"
        ):
            with self.assertRaisesRegex(Blocked, "revoked"):
                revalidate(self.gh, resumed)

    def test_repository_pr_quorum_excludes_bots_readers_and_duplicate_reviewers(self):
        candidate, checks, reviewed = self.pr_checkpoint(reviewers="repository", minimum=2)
        self.review(candidate, login="outsider", number=20)
        bot = self.review(candidate, login="owner", number=21)
        bot["user"]["type"] = "Bot"
        self.assertFalse(self.resume()["ready"])
        self.review(candidate, login="second", number=22)
        self.review(candidate, login="second", number=23)
        self.assertFalse(self.resume()["ready"])
        self.review(candidate, login="owner", number=24)
        result = self.resume()
        self.assertTrue(result["ready"])
        outcome = finish(self.gh, result["context"], combine_checks(candidate, checks), reviewed)
        self.assertEqual(outcome["status"], "merged")
        self.assertEqual(self.gh.state["agent_calls"], 2)

    def test_repository_policy_accepts_new_collaborators_by_current_permission(self):
        for permission in ("write", "maintain", "admin"):
            with self.subTest(permission=permission):
                candidate, _, _ = self.pr_checkpoint(reviewers="repository")
                self.review(candidate, login="new-member")
                self.assertFalse(self.resume()["ready"])
                with patch.object(
                    self.gh,
                    "permission",
                    side_effect=lambda login: permission if login == "new-member" else "admin",
                ):
                    resumed = self.resume()["context"]
                with self.assertRaisesRegex(Blocked, "revoked"):
                    revalidate(self.gh, resumed)

    def test_repository_policy_retains_exact_head_time_and_dismissal_rules(self):
        for change in ("stale", "early", "dismissed"):
            with self.subTest(change=change):
                candidate, _, _ = self.pr_checkpoint(reviewers="repository")
                review = self.review(candidate, login="second")
                if change == "stale":
                    review["commit_id"] = "f" * 40
                elif change == "early":
                    review["submitted_at"] = "2020-01-01T00:00:00Z"
                else:
                    self.review(candidate, status="DISMISSED", login="second", number=21)
                self.assertFalse(self.resume()["ready"])
                self.assertFalse(self.gh.merges)

    def test_repository_reviewer_changes_trigger_bounded_repair(self):
        candidate, _, _ = self.pr_checkpoint(reviewers="repository")
        self.review(candidate, status="CHANGES_REQUESTED", login="second")
        result = self.resume()
        self.assertEqual(result["status"], "retry")
        self.assertIn("Check the boundary case", self.gh.state["feedback"]["reason"])
        self.assertEqual(self.gh.state["agent_calls"], 2)
        self.assertEqual(len(self.gh.dispatches), 1)
        self.assertFalse(self.gh.merges)

    def test_interrupted_review_followup_does_not_repeat_previous_ai_approval(self):
        candidate, _, _ = self.pr_checkpoint(reviewers="repository")
        self.review(candidate, status="CHANGES_REQUESTED", login="second")
        with patch("nexkit.approvals.repair", side_effect=Blocked("Interrupted before repair")):
            with self.assertRaisesRegex(Blocked, "Interrupted before repair"):
                self.resume()
        self.assertEqual(self.gh.state["approval_repair"]["status"], "pending")
        message = state_message(1, self.gh.state)
        self.assertIn("Review follow-up pending", message)
        self.assertIn("Check the boundary case", message)
        self.assertNotIn("audit (approve)", message)

    def test_approved_continuation_reports_the_next_step(self):
        candidate, _, _ = self.pr_checkpoint(reviewers="repository")
        self.review(candidate, login="second")
        self.assertTrue(self.resume()["ready"])
        self.assertIn("Approval received; continuing", state_message(1, self.gh.state))

    def test_named_reviewers_remain_an_explicit_restriction(self):
        candidate, _, _ = self.pr_checkpoint()
        self.review(candidate, login="second")
        self.assertFalse(self.resume()["ready"])
        self.review(candidate, login="owner", number=21)
        self.assertTrue(self.resume()["ready"])

    def test_review_notice_has_concise_result_and_configured_policy(self):
        self.pr_checkpoint(reviewers="repository")
        body = self.gh.messages[-1]
        self.assertIn("Awaiting PR review", body)
        self.assertIn("Implemented and checked signed integer sum", body)
        self.assertIn("Required checks passed", body)
        self.assertIn("1 from repository collaborators with write, maintain or admin access", body)
        self.assertIn("24 hours (1,440 minutes), configured for this project", body)
        self.assertIn("<summary>Approval details</summary>", body)
        self.assertNotIn("human", body)

    def test_skipping_the_gate_cannot_reserve_its_protected_agent(self):
        with self.assertRaisesRegex(Blocked, "required before"):
            invocations.prepare(self.gh, self.context, "edit")
        self.assertEqual(self.gh.state["agent_calls"], 0)

    def test_wait_finishes_without_spending_another_round_or_call(self):
        outcome = self.request()
        state = deepcopy(self.gh.state)
        self.assertFalse(outcome["ready"])
        self.assertEqual(state["status"], "waiting_for_approval")
        self.assertFalse(
            prepare(self.gh, 1, "201.1", "a" * 40, pipeline="maintenance", individual_agents=True)[
                "ready"
            ]
        )
        self.assertEqual(failed(self.gh, self.context, "Skipped downstream jobs"), state)
        self.assertEqual(self.gh.state, state)
        self.assertFalse(self.gh.dispatches)

    def test_exact_human_command_is_required_and_edited_or_wrong_actor_does_not_count(self):
        self.request()
        wrong = self.command(login="outsider")
        self.assertFalse(self.resume()["ready"])
        wrong["user"] = {"login": "owner", "type": "Bot"}
        self.assertFalse(self.resume()["ready"])
        valid = self.command()
        valid["updated_at"] = (
            datetime.fromisoformat(valid["created_at"]) + timedelta(seconds=1)
        ).isoformat()
        self.assertFalse(self.resume()["ready"])
        valid["updated_at"] = valid["created_at"]
        valid["body"] = "approved"
        self.assertFalse(self.resume()["ready"])
        self.assertEqual(self.gh.state["agent_calls"], 0)

    def test_resume_preserves_origin_budgets_and_exact_recorded_input(self):
        task = invocations.prepare(self.gh, self.context, "inspect")
        output = report(task)
        invocations.record(self.gh, task, output)
        self.request(inputs=[output])
        old = deepcopy(self.gh.state)
        self.command()
        resumed = self.resume()["context"]
        invocations.runtime_guard(self.gh, resumed, "a" * 40)
        for field in ("run_key", "attempts", "agent_calls", "started_at", "reservations"):
            self.assertEqual(self.gh.state[field], old[field])
        editor = invocations.prepare(self.gh, resumed, "edit")
        self.assertEqual(editor["previous_outputs"], [output])
        self.assertEqual(output["run_key"], "100.1")
        self.assertEqual(self.gh.state["agent_calls"], 2)
        with self.assertRaises(Blocked):
            invocations.runtime_guard(self.gh, self.context, "a" * 40)
        with self.assertRaises(Blocked):
            failed(self.gh, self.context, "Old finalizer")

    def test_human_wait_does_not_exhaust_delivery_elapsed_limit(self):
        self.request()
        record = self.gh.state["stage_approvals"]["owner-check"]
        later = (datetime.fromisoformat(record["opened_at"]) + timedelta(hours=5)).isoformat()
        with (
            patch("nexkit.approvals.now", return_value=later),
            patch("nexkit.policy.now", return_value=later),
            patch("nexkit.delivery.now", return_value=later),
        ):
            comment = self.command()
            comment["created_at"] = comment["updated_at"] = later
            result = self.resume()
            self.assertTrue(result["ready"], result)
            self.assertAlmostEqual(self.gh.state["human_wait_seconds"], 18000, delta=1)
            revalidate(self.gh, result["context"])
            before = self.gh.state["human_wait_seconds"]
            duplicate = self.resume()
            self.assertTrue(duplicate["ready"])
            self.assertEqual(self.gh.state["human_wait_seconds"], before)

    def test_timeout_blocks_without_an_agent_and_requires_fresh_explicit_resume(self):
        self.request()
        record = self.gh.state["stage_approvals"]["owner-check"]
        later = (datetime.fromisoformat(record["opened_at"]) + timedelta(days=2)).isoformat()
        self.command()
        with (
            patch("nexkit.approvals.now", return_value=later),
            patch("nexkit.policy.now", return_value=later),
        ):
            result = self.resume()
            self.assertEqual(result["status"], "blocked")
            self.assertIn("expired", result["reason"])
            self.assertFalse(
                prepare(
                    self.gh, 1, "300.1", "a" * 40, pipeline="maintenance", individual_agents=True
                )["ready"]
            )
        self.assertEqual(self.gh.state["agent_calls"], 0)
        self.assertFalse(self.gh.dispatches)

    def test_changed_requirement_base_and_control_cannot_continue(self):
        for change in ("spec", "base", "control"):
            with self.subTest(change=change):
                self.gh = ApprovalGitHub()
                os.environ["GITHUB_WORKFLOW_REF"] = (
                    "owner/project/.github/workflows/changes.yml@refs/heads/main"
                )
                self.context = self.start()
                self.request()
                self.command()
                if change == "spec":
                    self.gh.work["body"] += " Changed requirement."
                elif change == "base":
                    self.gh.branches["main"] = "e" * 40
                else:
                    self.gh.files[".github/workflows/continue.yml"] += b"# changed\n"
                try:
                    self.assertFalse(self.resume()["ready"])
                except Blocked:
                    pass
                self.assertEqual(self.gh.state["agent_calls"], 0)
                self.assertFalse(self.gh.merges)

    def test_modified_or_unrecorded_stage_result_cannot_be_checkpointed(self):
        task = invocations.prepare(self.gh, self.context, "inspect")
        output = report(task)
        with self.assertRaises(Blocked):
            self.request(inputs=[output])
        invocations.record(self.gh, task, output)
        output["result"]["summary"] = "Substituted result"
        with self.assertRaisesRegex(Blocked, "recorded"):
            self.request(inputs=[output])

    def test_revoked_stage_approval_stops_further_agent_work(self):
        self.request()
        comment = self.command()
        context = self.resume()["context"]
        self.gh.discussion.remove(comment)
        with self.assertRaisesRegex(Blocked, "revoked"):
            invocations.prepare(self.gh, context, "edit")
        self.assertEqual(self.gh.state["agent_calls"], 0)

    def test_request_changes_dispatches_one_bounded_repair_with_feedback(self):
        self.request()
        self.command(changes="Explain the overflow handling.")
        result = self.resume()
        self.assertEqual(result["status"], "retry")
        self.assertIn("overflow handling", self.gh.state["feedback"]["reason"])
        self.assertEqual(len(self.gh.dispatches), 1)
        self.assertFalse(self.resume("201.1")["ready"])
        self.assertEqual(len(self.gh.dispatches), 1)
        self.assertEqual(self.gh.state["agent_calls"], 0)
        self.assertEqual(self.gh.state["attempts"], 1)

    def test_explicit_rejection_policy_stops_without_a_repair(self):
        self.gh.cfg["pipelines"]["maintenance"]["approvals"]["owner-check"]["on_rejection"] = (
            "block"
        )
        self.context = self.start("101.1")
        self.request()
        self.command(changes="Stop this candidate.")
        self.assertEqual(self.resume()["status"], "blocked")
        self.assertFalse(self.gh.dispatches)
        self.assertIn("human_stop", self.gh.state)

    def test_blocked_human_feedback_survives_explicit_delivery_recovery(self):
        self.test_explicit_rejection_policy_stops_without_a_repair()
        stamp = (
            datetime.fromisoformat(self.gh.state["human_stop"]) + timedelta(seconds=1)
        ).isoformat()
        self.gh.discussion.append(
            {
                "id": 4000,
                "body": "/nexkit resume",
                "created_at": stamp,
                "updated_at": stamp,
                "user": {"login": "owner", "type": "User"},
            }
        )
        os.environ["GITHUB_WORKFLOW_REF"] = (
            "owner/project/.github/workflows/changes.yml@refs/heads/main"
        )
        context = self.start("300.1")
        self.assertIn("Stop this candidate.", context["feedback"]["reason"])
        self.assertFalse(self.gh.state["stage_approvals"])

    def test_request_changes_accepts_windows_line_endings(self):
        self.request()
        comment = self.command(changes="Check overflow.\nKeep signed values.")
        comment["body"] = comment["body"].replace("\n", "\r\n")
        self.assertEqual(self.resume()["status"], "retry")
        self.assertIn("Check overflow.\nKeep signed values.", self.gh.state["feedback"]["reason"])

    def test_notice_failure_leaves_recoverable_checkpoint_and_no_duplicate_notice(self):
        with patch.object(self.gh, "comment", side_effect=Blocked("Network unavailable")):
            with self.assertRaises(Blocked):
                self.request()
        self.assertEqual(self.gh.state["status"], "waiting_for_approval")
        self.assertFalse(self.resume()["ready"])
        count = len(self.gh.messages)
        self.assertFalse(self.resume()["ready"])
        self.assertEqual(len(self.gh.messages), count)
        self.assertEqual(self.gh.state["agent_calls"], 0)

    def test_claim_response_loss_recovers_without_repeating_wait_credit(self):
        self.request()
        self.command()
        first = self.resume()
        before = self.gh.state["human_wait_seconds"]
        second = self.resume("201.1")
        self.assertTrue(second["ready"])
        self.assertEqual(self.gh.state["human_wait_seconds"], before)
        self.assertEqual(first["context"]["run_key"], second["context"]["run_key"])
        with self.assertRaises(Blocked):
            invocations.runtime_guard(self.gh, first["context"], "a" * 40)
        invocations.runtime_guard(self.gh, second["context"], "a" * 40)

    def test_native_rerun_recovers_a_completed_attempt_of_the_same_run(self):
        self.request()
        self.command()
        self.resume("200.1")
        self.gh.run_status = "in_progress"
        path = f"{self.gh.root}/actions/runs/200/attempts/1"
        self.gh.attempt_statuses[path] = "completed"
        result = self.resume("200.2")
        self.assertTrue(result["ready"])
        self.assertIn(path, self.gh.run_queries)
        self.assertEqual(result["context"]["continuation"]["run_key"], "200.2")
        self.assertEqual(self.gh.state["attempts"], 1)
        self.assertEqual(self.gh.state["agent_calls"], 0)

    def test_delivery_does_not_spend_a_round_on_an_interrupted_approval_claim(self):
        self.request()
        self.command()
        self.resume()
        before = deepcopy(self.gh.state)
        self.assertEqual(approvals.recovery_gate(before), "owner-check")
        self.assertFalse(
            prepare(self.gh, 1, "300.1", "a" * 40, pipeline="maintenance", individual_agents=True)[
                "ready"
            ]
        )
        self.assertEqual(self.gh.state, before)

    def test_changed_decision_on_an_unused_claim_starts_bounded_repair(self):
        self.request()
        self.command()
        self.resume()
        self.command(changes="Handle decimals before continuing.", number=2001)
        result = self.resume("201.1")
        self.assertEqual(result["status"], "retry")
        self.assertIn("Handle decimals", self.gh.state["feedback"]["reason"])
        self.assertEqual(len(self.gh.dispatches), 1)
        self.assertEqual(self.gh.state["attempts"], 1)
        self.assertEqual(self.gh.state["agent_calls"], 0)

    def test_unused_claim_revocation_and_subject_drift_leave_recoverable_stops(self):
        for change in ("revoke", "spec", "base", "control"):
            with self.subTest(change=change):
                self.gh = ApprovalGitHub()
                os.environ["GITHUB_WORKFLOW_REF"] = (
                    "owner/project/.github/workflows/changes.yml@refs/heads/main"
                )
                self.context = self.start()
                self.request()
                comment = self.command()
                self.resume()
                if change == "revoke":
                    self.gh.discussion.remove(comment)
                elif change == "spec":
                    self.gh.work["body"] += " Changed."
                elif change == "base":
                    self.gh.branches["main"] = "e" * 40
                else:
                    self.gh.files[".github/workflows/continue.yml"] += b"# changed\n"
                result = self.resume("201.1")
                self.assertEqual(result["status"], "blocked")
                self.assertIn("human_stop", self.gh.state)
                self.assertIsNone(approvals.recovery_gate(self.gh.state))
                self.assertEqual(self.gh.state["attempts"], 1)
                self.assertEqual(self.gh.state["agent_calls"], 0)
                self.assertFalse(self.gh.dispatches)

    def test_unrelated_continuation_cannot_mutate_wait_claim_or_repair(self):
        self.request()
        for phase in ("wait", "claim", "repair"):
            if phase == "claim":
                self.command()
                self.resume()
            elif phase == "repair":
                self.command(changes="Revise the plan.", number=2001)
                with patch.object(self.gh, "dispatch", side_effect=Blocked("Unavailable")):
                    with self.assertRaises(Blocked):
                        self.resume("201.1")
            for wrong in ("pipeline", "workflow"):
                before = deepcopy(self.gh.state)
                os.environ["GITHUB_WORKFLOW_REF"] = (
                    "owner/project/.github/workflows/"
                    + ("changes.yml" if wrong == "workflow" else "continue.yml")
                    + "@refs/heads/main"
                )
                with self.subTest(phase=phase, wrong=wrong), self.assertRaises(Blocked):
                    approvals.resume(
                        self.gh,
                        1,
                        "another" if wrong == "pipeline" else "maintenance",
                        "owner-check",
                        "a" * 40,
                        "299.1",
                    )
                self.assertEqual(self.gh.state, before)

    def test_repair_configuration_drift_leaves_a_recoverable_stop(self):
        self.request()
        self.command(changes="Revise the plan.")
        with patch.object(self.gh, "dispatch", side_effect=Blocked("Unavailable")):
            with self.assertRaises(Blocked):
                self.resume()
        self.gh.files[".github/workflows/continue.yml"] += b"# changed\n"
        self.assertEqual(self.resume("201.1")["status"], "blocked")
        self.assertIsNone(approvals.recovery_gate(self.gh.state))
        self.assertIn("Revise the plan.", self.gh.state["feedback"]["reason"])

    def test_pr_review_resumes_to_merge_without_another_model_call(self):
        candidate, checks, reviewed = self.pr_checkpoint()
        self.review(candidate)
        prior = self.gh.state["agent_calls"]
        resumed = self.resume()["context"]
        self.assertEqual(self.gh.state["agent_calls"], prior)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "context.json", resumed)
            with (
                patch("nexkit.ci.GitHub", return_value=self.gh),
                patch(
                    "sys.argv",
                    [
                        "ci",
                        "finish-work",
                        "--context",
                        str(root / "context.json"),
                        "--kit-ref",
                        "a" * 40,
                        "--jobs-succeeded",
                        "--out",
                        str(root / "out.json"),
                    ],
                ),
                patch("sys.stdout", new=io.StringIO()),
            ):
                self.assertEqual(main(), 0)
            self.assertEqual(read_json(root / "out.json")["status"], "merged")
        self.assertEqual(len(self.gh.merges), 1)
        self.assertEqual(self.gh.state["agent_calls"], prior)
        self.assertEqual(reviewed["run_key"], "100.1")
        self.assertEqual(checks[0]["producer"]["run_key"], "100.1")

    def test_pr_review_reducer_handles_wrong_head_changes_comments_and_dismissal(self):
        candidate, _, _ = self.pr_checkpoint()
        value = self.review(candidate)
        value["commit_id"] = "f" * 40
        self.assertFalse(self.resume()["ready"])
        value["state"] = "CHANGES_REQUESTED"
        value["commit_id"] = candidate["candidate"]["head"]
        self.review(candidate, status="COMMENTED", number=21)
        record = self.gh.state["stage_approvals"]["owner-check"]
        self.assertEqual(approvals.decision(self.gh, record)["status"], "changes_requested")
        value["state"] = "DISMISSED"
        self.assertEqual(approvals.decision(self.gh, record)["status"], "waiting")
        self.review(candidate, number=22)
        self.assertEqual(approvals.decision(self.gh, record)["status"], "approved")

    def test_pr_changes_include_inline_comments_in_bounded_repair(self):
        candidate, _, _ = self.pr_checkpoint()
        self.review(candidate, status="CHANGES_REQUESTED")
        self.gh.inline = [{"path": "sum.py", "body": "Preserve large integer precision."}]
        result = self.resume()
        self.assertEqual(result["status"], "retry")
        self.assertIn("large integer precision", self.gh.state["feedback"]["reason"])
        self.assertEqual(len(self.gh.dispatches), 1)

    def test_pr_review_relay_recovers_an_unused_claim_with_new_inline_feedback(self):
        candidate, _, _ = self.pr_checkpoint()
        self.review(candidate)
        self.resume()
        self.review(candidate, status="CHANGES_REQUESTED", number=21)
        self.gh.inline = [{"path": "sum.py", "body": "Handle the boundary before merging."}]
        event = {"pull_request": deepcopy(self.gh.pr), "sender": {"login": "owner", "type": "User"}}
        self.assertTrue(approvals.relay_review(self.gh, event, "a" * 40)["dispatched"])
        self.assertEqual(self.resume("201.1")["status"], "retry")
        self.assertIn("Handle the boundary", self.gh.state["feedback"]["reason"])
        self.assertEqual(len(self.gh.dispatches), 2)
        self.assertEqual(self.gh.state["agent_calls"], 2)

    def test_candidate_drift_after_human_approval_cannot_merge(self):
        candidate, checks, reviewed = self.pr_checkpoint()
        self.review(candidate)
        context = self.resume()["context"]
        self.gh.pr["head"]["sha"] = "f" * 40
        outcome = finish(self.gh, context, combine_checks(candidate, checks), reviewed)
        self.assertNotEqual(outcome["status"], "merged")
        self.assertFalse(self.gh.merges)

    def test_native_pr_relay_only_dispatches_accepted_default_branch(self):
        candidate, _, _ = self.pr_checkpoint()
        event = {"pull_request": deepcopy(self.gh.pr), "sender": {"login": "owner", "type": "User"}}
        result = approvals.relay_review(self.gh, event, "a" * 40)
        self.assertTrue(result["dispatched"])
        self.assertEqual(self.gh.dispatches, [("continue.yml", "main", {"issue": 1})])
        event["pull_request"]["head"]["repo"]["full_name"] = "outsider/fork"
        self.assertFalse(approvals.relay_review(self.gh, event, "a" * 40)["dispatched"])
        self.assertEqual(self.gh.state["status"], "waiting_for_approval")

    def test_check_continuation_requires_the_recorded_execution_and_protected_gate(self):
        self.gh = ApprovalGitHub(subject="candidate", protects=["check:test"])
        self.context = self.start()
        candidate = self.candidate()
        self.request(candidate)
        self.command()
        context = self.resume()["context"]
        with patch("nexkit.ci.GitHub", return_value=self.gh):
            self.assertTrue(prepare_check(context, "test", "a" * 40)["authorized"])
            with self.assertRaises(Blocked):
                prepare_check(candidate, "test", "a" * 40)

    def test_multiple_approvals_require_distinct_authorized_humans(self):
        definition = self.gh.cfg["pipelines"]["maintenance"]["approvals"]["owner-check"]
        definition.update(reviewers=["owner", "second"], minimum=2)
        self.context = self.start("101.1")
        self.request()
        self.command()
        self.command(number=2001)
        self.assertFalse(self.resume()["ready"])
        self.command(login="second", number=2002)
        self.assertTrue(self.resume()["ready"])

    def test_checkpoint_data_tampering_invalidates_the_human_command(self):
        self.request()
        self.command()
        record = self.gh.state["stage_approvals"]["owner-check"]
        record["data"]["inputs"] = [{"result": {"summary": "Substituted plan"}}]
        with self.assertRaisesRegex(Blocked, "checkpoint content changed"):
            self.resume()
        self.assertEqual(self.gh.state["agent_calls"], 0)

    def test_two_gates_continue_one_round_and_keep_earlier_plan_approval_valid(self):
        definition = deepcopy(self.gh.cfg["pipelines"]["maintenance"]["approvals"]["owner-check"])
        definition.update(subject="candidate", mode="pull_request", protects=["merge"])
        self.gh.cfg["pipelines"]["maintenance"]["approvals"]["final-review"] = definition
        self.context = self.start("101.1")
        task = invocations.prepare(self.gh, self.context, "inspect")
        plan = report(task)
        invocations.record(self.gh, task, plan)
        self.request(inputs=[plan])
        self.command()
        self.context = self.resume()["context"]
        candidate = self.candidate()
        checks = check_reports(candidate)
        reviewer = invocations.prepare(self.gh, candidate, "audit", check_reports=checks)
        reviewed = report(reviewer)
        invocations.record(self.gh, reviewer, reviewed)
        approvals.request(self.gh, candidate, "final-review", checks=checks, review=reviewed)
        self.review(candidate)
        os.environ["GITHUB_RUN_ID"] = "300"
        resumed = approvals.resume(self.gh, 1, "maintenance", "final-review", "a" * 40, "300.1")[
            "context"
        ]
        invocations.runtime_guard(self.gh, resumed, "a" * 40)
        outcome = finish(self.gh, resumed, combine_checks(candidate, checks), reviewed)
        self.assertEqual(outcome["status"], "merged")
        self.assertEqual(outcome["attempts"], 2)
        self.assertEqual(outcome["agent_calls"], 3)
        self.assertEqual(outcome["stage_approvals"]["owner-check"]["status"], "approved")

    def test_cancel_during_wait_prevents_any_continuation(self):
        self.request()
        self.command()
        stamp = now()
        self.gh.discussion.append(
            {
                "id": 3000,
                "body": "/nexkit cancel",
                "created_at": stamp,
                "updated_at": stamp,
                "user": {"login": "owner", "type": "User"},
            }
        )
        result = self.resume()
        self.assertEqual(result["status"], "blocked")
        self.assertIn("cancelled", result["reason"])
        self.assertEqual(self.gh.state["agent_calls"], 0)

    def test_resume_claim_cannot_be_taken_while_its_execution_is_active(self):
        self.request()
        self.command()
        self.resume()
        self.gh.run_status = "in_progress"
        before = deepcopy(self.gh.state)
        self.assertFalse(self.resume("201.1")["ready"])
        self.assertEqual(self.gh.state, before)

    def test_revocation_at_final_merge_does_not_trigger_ai_repair(self):
        candidate, checks, reviewed = self.pr_checkpoint()
        value = self.review(candidate)
        context = self.resume()["context"]
        value["state"] = "DISMISSED"
        outcome = finish(self.gh, context, combine_checks(candidate, checks), reviewed)
        self.assertEqual(outcome["status"], "blocked")
        self.assertFalse(self.gh.dispatches)
        self.assertFalse(self.gh.merges)

    def test_exhausted_repair_budget_is_not_reset_by_human_feedback(self):
        candidate, _, _ = self.pr_checkpoint()
        self.gh.state["attempts"] = self.context["config"]["limits"]["attempts"]
        self.review(candidate, status="CHANGES_REQUESTED")
        result = self.resume()
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(self.gh.dispatches)
        self.assertEqual(self.gh.state["agent_calls"], 2)

    def test_merge_followed_by_state_write_failure_is_reconciled(self):
        candidate, checks, reviewed = self.pr_checkpoint()
        self.review(candidate)
        context = self.resume()["context"]
        save = self.gh.save_state

        def lose_after_merge(number, value, previous):
            if value.get("status") == "merged":
                raise Blocked("Lost state update after merge")
            return save(number, value, previous)

        with patch.object(self.gh, "save_state", side_effect=lose_after_merge):
            with self.assertRaises(Blocked):
                finish(self.gh, context, combine_checks(candidate, checks), reviewed)
        result = self.resume("201.1")
        self.assertEqual(result["status"], "merged")
        self.assertEqual(len(self.gh.merges), 1)

    def test_completed_editor_must_be_published_before_a_human_wait(self):
        self.gh.cfg["pipelines"]["maintenance"]["approvals"]["owner-check"]["protects"] = ["merge"]
        self.context = self.start("101.1")
        editor = invocations.prepare(self.gh, self.context, "edit")
        output = report(editor)
        invocations.record(self.gh, editor, output)
        with self.assertRaisesRegex(Blocked, "Publish source changes"):
            self.request(editor, inputs=[output])

    def test_native_required_pr_reviews_match_the_configured_policy(self):
        from nexkit.github import GitHub
        from tests.support import project

        cfg = effective_config(
            ApprovalGitHub("pull_request", subject="candidate", protects=["merge"]).cfg,
            "maintenance",
        )
        rules = [
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": True,
                    "required_status_checks": [
                        {"context": name, "integration_id": 15368}
                        for name in ("NexKit verification", "NexKit review")
                    ],
                },
            },
            {
                "type": "pull_request",
                "parameters": {
                    "required_approving_review_count": 1,
                    "dismiss_stale_reviews_on_push": True,
                },
            },
        ]
        gh = GitHub("owner/project")

        def api(path, *args, **kwargs):
            return {"id": 15368} if path == "apps/github-actions" else deepcopy(rules)

        with patch.object(gh, "api", side_effect=api):
            gh.strict_protection("main", cfg)
            with self.assertRaises(Blocked):
                gh.strict_protection("main", project())
            rules[1]["parameters"]["dismiss_stale_reviews_on_push"] = False
            with self.assertRaises(Blocked):
                gh.strict_protection("main", cfg)

    def test_repair_intent_recovers_lost_state_response_and_dispatch_failure(self):
        self.request()
        self.command(changes="Explain the integer boundary.")
        save = self.gh.save_state
        lost = False

        def lose_once(number, value, previous):
            nonlocal lost
            result = save(number, value, previous)
            if value.get("approval_repair") and not lost:
                lost = True
                raise Blocked("Lost response after persisting repair intent")
            return result

        with patch.object(self.gh, "save_state", side_effect=lose_once):
            with self.assertRaises(Blocked):
                self.resume()
        self.assertIn("approval_repair", self.gh.state)
        before = self.gh.state["human_wait_seconds"]
        with patch.object(self.gh, "dispatch", side_effect=Blocked("Dispatch unavailable")):
            with self.assertRaises(Blocked):
                self.resume("201.1")
        self.assertEqual(self.gh.state["status"], "retry")
        self.assertIn("integer boundary", self.gh.state["feedback"]["reason"])
        before_state = deepcopy(self.gh.state)
        self.assertEqual(approvals.recovery_gate(before_state), "owner-check")
        self.assertFalse(
            prepare(self.gh, 1, "299.1", "a" * 40, pipeline="maintenance", individual_agents=True)[
                "ready"
            ]
        )
        self.assertEqual(self.gh.state, before_state)
        result = self.resume("202.1")
        self.assertEqual(result["status"], "retry")
        self.assertEqual(self.gh.state["approval_repair"]["status"], "dispatched")
        self.resume("203.1")
        self.assertEqual(len(self.gh.dispatches), 1)
        self.assertEqual(self.gh.state["human_wait_seconds"], before)
        self.assertEqual(self.gh.state["agent_calls"], 0)

    def test_unconfigured_native_blocking_review_waits_without_ai_repair_authority(self):
        candidate, _, _ = self.pr_checkpoint()
        blocker = self.review(candidate, status="CHANGES_REQUESTED", login="second", number=19)
        self.review(candidate)
        result = self.resume()
        self.assertFalse(result["ready"])
        self.assertIn("another authorized collaborator", result["reason"])
        self.assertFalse(self.gh.dispatches)
        blocker["state"] = "DISMISSED"
        self.assertTrue(self.resume()["ready"])

    def test_mixed_branch_review_policies_and_impossible_gate_dependencies_fail_setup(self):
        value = ApprovalGitHub("pull_request", subject="candidate", protects=["merge"]).cfg
        cfg = deepcopy(value)
        cfg["pipelines"]["another-delivery"] = deepcopy(cfg["pipelines"]["maintenance"])
        cfg["pipelines"]["another-delivery"].pop("approvals")
        with self.assertRaisesRegex(Blocked, "same native PR approval count"):
            config(cfg)
        for protects in (
            ["publish"],
            ["check:test"],
            ["invocation:audit"],
            ["invocation:edit", "invocation:polish"],
        ):
            cfg = deepcopy(value)
            cfg["pipelines"]["maintenance"]["approvals"]["owner-check"]["protects"] = protects
            with self.subTest(protects=protects), self.assertRaises(Blocked):
                config(cfg)

    def test_missing_check_approval_is_preserved_when_finalizer_has_incomplete_reports(self):
        self.gh = ApprovalGitHub(subject="candidate", protects=["check:test"])
        self.context = self.start()
        candidate = self.candidate()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "round.json", self.context)
            write_json(root / "candidate.json", candidate)
            with (
                patch("nexkit.ci.GitHub", return_value=self.gh),
                patch("sys.stdout", new=io.StringIO()),
            ):
                with patch(
                    "sys.argv",
                    [
                        "ci",
                        "prepare-check",
                        "--context",
                        str(root / "candidate.json"),
                        "--check",
                        "test",
                        "--kit-ref",
                        "a" * 40,
                        "--out",
                        str(root / "denied.json"),
                    ],
                ):
                    self.assertEqual(main(), 0)
                denied = read_json(root / "denied.json")
                self.assertFalse(denied["passed"])
                self.assertFalse(denied["checks"][0]["executed"])
                with patch(
                    "sys.argv",
                    [
                        "ci",
                        "finish-work",
                        "--context",
                        str(root / "round.json"),
                        "--candidate",
                        str(root / "candidate.json"),
                        "--reports",
                        str(root / "denied.json"),
                        "--kit-ref",
                        "a" * 40,
                        "--out",
                        str(root / "outcome.json"),
                    ],
                ):
                    self.assertEqual(main(), 0)
            outcome = read_json(root / "outcome.json")
            self.assertEqual(outcome["status"], "blocked")
            self.assertEqual(outcome["approval_denial"]["capability"], "check:test")
            self.assertIn("human_stop", outcome)
            self.assertFalse(self.gh.dispatches)
            self.assertEqual(self.gh.state["agent_calls"], 1)

    def test_notice_and_human_feedback_have_aggregate_bounds(self):
        self.request()
        state = deepcopy(self.gh.state)
        record = state["stage_approvals"]["owner-check"]
        record["data"]["inputs"] = [{"result": {"summary": "A" * 6000}} for _ in range(12)]
        record["digest"] = "f" * 64
        approvals.notice(self.gh, state)
        self.assertLess(len(self.gh.messages[-1]), 30000)
        self.assertIn("Full persisted checkpoint", self.gh.messages[-1])

    def test_large_quorum_feedback_keeps_state_readable(self):
        definition = self.gh.cfg["pipelines"]["maintenance"]["approvals"]["owner-check"]
        definition.update(reviewers=[f"owner-{x}" for x in range(50)], on_rejection="block")
        self.context = self.start("101.1")
        task = invocations.prepare(self.gh, self.context, "inspect")
        output = report(task)
        output["result"]["summary"] = "A" * 680000
        invocations.record(self.gh, task, output)
        self.request(inputs=[output])
        self.assertLess(len(canonical(self.gh.state).encode()), 750000)
        for index in range(50):
            self.command(changes="\u2603" * 6000, login=f"owner-{index}", number=2000 + index)
        with patch.object(self.gh, "permission", return_value="admin"):
            self.assertEqual(self.resume()["status"], "blocked")
        self.assertLess(len(canonical(self.gh.state).encode()), 900000)
        self.assertLessEqual(len(self.gh.state["feedback"]["reason"].encode()), 24000)
        receipts = self.gh.state["stage_approvals"]["owner-check"]["decision"]["reviews"]
        self.assertEqual(len(receipts), 50)
        self.assertTrue(all("feedback" not in x for x in receipts))

    def test_state_writer_rejects_unreadable_size_before_any_api_write(self):
        from nexkit.github import GitHub

        gh = GitHub("owner/project")
        with patch.object(gh, "api") as api:
            with self.assertRaisesRegex(Blocked, "bounded GitHub state"):
                gh.save_state(1, {"feedback": "A" * 900000}, None)
            api.assert_not_called()

    def test_native_workflows_keep_waiting_out_of_agent_jobs(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("request-approval", "resume-approval", "review-events"):
            workflow = yaml.safe_load((root / ".github/workflows" / (name + ".yml")).read_text())
            for job in workflow["jobs"].values():
                self.assertEqual(job["runs-on"], "ubuntu-24.04")
                self.assertNotIn("environment", job)
                self.assertNotIn("OPENAI_API_KEY", str(job))
        relay = (root / ".github/workflows/review-events.yml").read_text()
        self.assertNotIn("path: source", relay)
        self.assertIn("nexkit.ci review-event", relay)
        for name in ("agent-invocation", "candidate-check"):
            workflow = yaml.safe_load((root / ".github/workflows" / (name + ".yml")).read_text())
            for job in workflow["jobs"].values():
                self.assertEqual(job["permissions"]["pull-requests"], "read")


if __name__ == "__main__":
    unittest.main()
