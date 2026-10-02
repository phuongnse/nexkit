"""Issue completion and recovery with simulated GitHub, never live approvals."""

import os
import unittest
from copy import deepcopy
from unittest.mock import patch

from nexkit import completion
from nexkit.common import Blocked
from nexkit.delivery import finish, prepare, publish
from nexkit.policy import now
from tests.support import FakeGitHub, approve, reviewed, verified, workflow_environment
from tests.test_delivery import bundle


class CompletionGitHub(FakeGitHub):
    def __init__(self):
        super().__init__()
        self.extra = {
            3: {
                "number": 3,
                "title": "Preserve integer signs",
                "body": "Negative totals stay negative.",
                "state": "open",
                "created_at": "2000-01-01T00:00:00Z",
                "last_edited_at": None,
            },
        }
        self.other_states = {}
        self.events = {}
        self.close_requests = []

    def issue(self, number):
        return super().issue(number) if number == 1 else deepcopy(self.extra[number])

    def get_state(self, number):
        if number != 1:
            return deepcopy(self.other_states.get(number, {})), None
        return super().get_state(number)

    def comment(self, number, body):
        super().comment(number, body)
        self.discussion.append(
            {"body": body, "user": {"login": "github-actions[bot]", "type": "Bot"}}
        )

    def api(self, path, method="GET", data=None, **kwargs):
        if path.endswith("/timeline?per_page=100"):
            number = int(path.split("/issues/")[1].split("/")[0])
            return deepcopy(self.events.get(number, []))
        if path.startswith(self.root + "/issues/") and method == "PATCH":
            number = int(path.rsplit("/", 1)[-1])
            self.close_requests.append(number)
            if number != 1:
                self.extra[number].update(deepcopy(data))
                return deepcopy(self.extra[number])
        return super().api(path, method, data, **kwargs)


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, workflow_environment()))
        self.gh = CompletionGitHub()

    def candidate(self, scope=None):
        if scope is not None:
            self.gh.work["body"] += "\n\n## Issues to close\n\n" + scope
            self.gh.discussion = [approve(self.gh.work)]
        context = prepare(self.gh, 1, "100.1", "a" * 40, pipeline="maintenance")
        self.assertTrue(context["ready"], context)
        return publish(self.gh, context, bundle(context))

    def review(self, context, *, extra=True):
        value = reviewed(context["candidate"])
        if extra:
            value["result"]["acceptance"].append(
                {
                    "criterion": "Issue #3 completion",
                    "passed": True,
                    "evidence": "The negative-input command returns a negative total.",
                }
            )
        return value

    def complete(self, context, *, extra=True):
        return finish(
            self.gh, context, verified(context["candidate"]), self.review(context, extra=extra)
        )

    def test_default_issue_closes_after_merge_with_readable_summary(self):
        context = self.candidate()
        self.assertEqual(self.gh.work["state"], "open")
        self.assertNotIn("Closes #", self.gh.pr["body"])
        self.assertIn("Refs #1", self.gh.pr["body"])
        state = self.complete(context)
        self.assertEqual(state["status"], "merged")
        self.assertEqual(state["completion"]["status"], "complete")
        self.assertEqual(self.gh.work["state"], "closed")
        self.assertEqual(self.gh.work["state_reason"], "completed")
        self.assertIn("Merged PR #2", self.gh.messages[-1])
        self.assertIn("does not publish a release", self.gh.messages[-1])
        before = self.gh.get_state(1), deepcopy(self.gh.messages), list(self.gh.close_requests)
        completion.reconcile(self.gh, 1)
        self.assertEqual((self.gh.get_state(1), self.gh.messages, self.gh.close_requests), before)

    def test_opt_out_and_explicit_empty_scope_keep_issues_open(self):
        for mode in ("disabled", "none"):
            with self.subTest(mode=mode):
                self.gh = CompletionGitHub()
                if mode == "disabled":
                    self.gh.cfg["issue_completion"] = {"close_after_merge": False}
                context = self.candidate("None." if mode == "none" else None)
                state = self.complete(context)
                self.assertEqual(state["status"], "merged")
                self.assertEqual(self.gh.work["state"], "open")
                self.assertFalse(self.gh.close_requests)
                self.assertEqual(
                    state["completion"]["status"], "disabled" if mode == "disabled" else "complete"
                )

    def test_explicit_multi_issue_scope_closes_only_completed_targets(self):
        context = self.candidate("- #1\n- #3")
        state = self.complete(context)
        self.assertEqual(state["status"], "merged")
        self.assertEqual(self.gh.close_requests, [1, 3])
        self.assertEqual(self.gh.extra[3]["state"], "closed")
        self.assertEqual(state["completion"]["review"]["candidate"], context["candidate"])

    def test_umbrella_issue_stays_open_when_scope_selects_only_its_child(self):
        context = self.candidate("- #3")
        self.complete(context)
        self.assertEqual(self.gh.close_requests, [3])
        self.assertEqual(self.gh.work["state"], "open")

    def test_missing_independent_completion_evidence_prevents_merge(self):
        context = self.candidate("- #1\n- #3")
        state = self.complete(context, extra=False)
        self.assertNotEqual(state["status"], "merged")
        self.assertIn("Issue #3 completion", state["reason"])
        self.assertFalse(self.gh.merges)
        self.assertFalse(self.gh.close_requests)

    def test_target_change_or_new_work_before_merge_invalidates_completion_scope(self):
        for mode in ("edit", "revert", "managed"):
            with self.subTest(mode=mode):
                self.gh = CompletionGitHub()
                context = self.candidate("- #3")
                if mode == "edit":
                    self.gh.extra[3]["body"] += " New scope."
                elif mode == "revert":
                    self.gh.extra[3]["last_edited_at"] = now()
                else:
                    self.gh.other_states[3] = {"status": "implementing"}
                state = self.complete(context)
                self.assertNotEqual(state["status"], "merged")
                self.assertFalse(self.gh.merges)
                self.assertFalse(self.gh.close_requests)

    def test_completion_denial_keeps_merge_and_supports_later_recovery(self):
        context = self.candidate()
        original = self.gh.api

        def deny(path, method="GET", data=None, **kwargs):
            if method == "PATCH" and "/issues/" in path:
                raise Blocked("HTTP 403: issue write denied")
            return original(path, method, data, **kwargs)

        with patch.object(self.gh, "api", side_effect=deny):
            state = self.complete(context)
        self.assertEqual(state["status"], "merged")
        self.assertEqual(state["completion"]["status"], "pending")
        self.assertEqual(self.gh.work["state"], "open")
        before = {
            key: deepcopy(state[key])
            for key in ("agent_calls", "attempts", "approval", "candidate", "merge_sha")
        }
        recovered = completion.reconcile(self.gh, 1)
        self.assertEqual(recovered["completion"]["status"], "complete")
        self.assertEqual({key: recovered[key] for key in before}, before)
        self.assertEqual(len(self.gh.merges), 1)

    def test_lost_closure_response_does_not_close_twice(self):
        context = self.candidate()
        original = self.gh.api

        def lost(path, method="GET", data=None, **kwargs):
            value = original(path, method, data, **kwargs)
            if method == "PATCH" and "/issues/" in path:
                raise Blocked("Lost close response")
            return value

        with patch.object(self.gh, "api", side_effect=lost):
            state = self.complete(context)
        self.assertEqual(state["completion"]["status"], "pending")
        recovered = completion.reconcile(self.gh, 1)
        self.assertEqual(recovered["completion"]["issues"]["1"]["status"], "already_closed")
        self.assertEqual(self.gh.close_requests, [1])

    def test_reopened_target_after_interruption_is_preserved(self):
        context = self.candidate()
        original = self.gh.api

        def denied(path, method="GET", data=None, **kwargs):
            if method == "PATCH" and "/issues/" in path:
                raise Blocked("HTTP 403")
            return original(path, method, data, **kwargs)

        with patch.object(self.gh, "api", side_effect=denied):
            self.complete(context)
        self.gh.events[1] = [{"event": "reopened", "created_at": now()}]
        state = completion.reconcile(self.gh, 1)
        self.assertEqual(state["completion"]["issues"]["1"]["status"], "kept_open")
        self.assertEqual(self.gh.work["state"], "open")
        self.assertFalse(self.gh.close_requests)

    def test_changed_target_after_merge_is_kept_open(self):
        context = self.candidate("- #3")
        original = self.gh.observe_merge

        def change_after_merge():
            original()
            self.gh.extra[3]["body"] += " Follow-up after merge."

        with patch.object(self.gh, "observe_merge", side_effect=change_after_merge):
            state = self.complete(context)
        self.assertEqual(state["status"], "merged")
        self.assertEqual(state["completion"]["issues"]["3"]["status"], "kept_open")
        self.assertFalse(self.gh.close_requests)

    def test_preview_of_pending_closure_has_no_external_effect(self):
        context = self.candidate()
        with patch(
            "nexkit.delivery.reconcile", side_effect=lambda gh, number: gh.get_state(number)[0]
        ):
            self.complete(context)
        before = deepcopy(self.gh.__dict__)
        preview = completion.reconcile(self.gh, 1, apply=False)
        self.assertEqual(preview["completion"]["issues"]["1"]["status"], "would_close")
        self.assertEqual(self.gh.__dict__, before)

    def test_already_completed_issue_reopened_later_is_never_closed_again(self):
        context = self.candidate()
        self.complete(context)
        self.gh.work["state"] = "open"
        self.gh.events[1] = [{"event": "reopened", "created_at": now()}]
        completion.reconcile(self.gh, 1)
        self.assertEqual(self.gh.work["state"], "open")
        self.assertEqual(self.gh.close_requests, [1])

    def test_pr_drift_or_missing_default_branch_ancestry_prevents_closure(self):
        for mode in ("head", "base", "merge", "ancestry"):
            with self.subTest(mode=mode):
                self.gh = CompletionGitHub()
                context = self.candidate()
                with patch(
                    "nexkit.delivery.reconcile",
                    side_effect=lambda gh, number: gh.get_state(number)[0],
                ):
                    self.complete(context)
                if mode == "head":
                    self.gh.pr["head"]["sha"] = "e" * 40
                elif mode == "base":
                    self.gh.pr["base"]["ref"] = "other"
                elif mode == "merge":
                    self.gh.pr["merge_commit_sha"] = "e" * 40
                else:
                    self.gh.branches["main"] = "b" * 40
                state = completion.reconcile(self.gh, 1)
                self.assertEqual(state["completion"]["status"], "pending")
                self.assertFalse(self.gh.close_requests)

    def test_missing_completion_scope_cannot_authorize_issue_closure(self):
        context = self.candidate("- #1\n- #3")
        with patch(
            "nexkit.delivery.reconcile", side_effect=lambda gh, number: gh.get_state(number)[0]
        ):
            self.complete(context)
        self.gh.state.pop("completion")
        self.gh.close_requests.clear()
        with self.assertRaisesRegex(Blocked, "missing its recorded completion scope"):
            completion.reconcile(self.gh, 1, apply=False)
        self.assertNotIn("completion", self.gh.state)
        with self.assertRaisesRegex(Blocked, "missing its recorded completion scope"):
            completion.reconcile(self.gh, 1)
        self.assertFalse(self.gh.close_requests)

    def test_independently_managed_release_pr_or_recent_target_is_rejected(self):
        for mode in ("managed", "release", "pr", "recent", "missing-time"):
            with self.subTest(mode=mode):
                self.gh = CompletionGitHub()
                self.gh.work["body"] += "\n## Issues to close\n- #3"
                self.gh.discussion = [approve(self.gh.work)]
                if mode == "managed":
                    self.gh.other_states[3] = {"status": "working"}
                elif mode == "release":
                    self.gh.extra[3]["body"] = "<!-- nexkit:release -->\nCandidate"
                elif mode == "pr":
                    self.gh.extra[3]["pull_request"] = {"url": "example"}
                elif mode == "recent":
                    self.gh.extra[3]["last_edited_at"] = now()
                else:
                    self.gh.extra[3].pop("created_at")
                context = prepare(self.gh, 1, "100.1", "a" * 40, pipeline="maintenance")
                self.assertFalse(context["ready"])
                self.assertFalse(self.gh.merges)
                self.assertEqual(self.gh.state.get("agent_calls", 0), 0)


class CompletionScopeTests(unittest.TestCase):
    def test_mentions_examples_and_comment_headings_do_not_grant_scope(self):
        for body in (
            "Related to #3.",
            "```markdown\n## Issues to close\n- #3\n```",
            "<!--\n## Issues to close\n- #3\n-->",
            "    ## Issues to close\n    - #3",
        ):
            with self.subTest(body=body):
                self.assertEqual(completion.selected_issues({"number": 1, "body": body}), [1])

    def test_scope_is_exact_bounded_and_unique(self):
        for scope in (
            "",
            "- #1\n- #1",
            "- #0",
            "- owner/repo#3",
            "- #3 and #4",
            "None.\n- #3",
            "- #3\n## Issues to close\n- #1",
            "\n".join(f"- #{number}" for number in range(1, 22)),
        ):
            with self.subTest(scope=scope), self.assertRaises(Blocked):
                completion.selected_issues({"number": 1, "body": "## Issues to close\n" + scope})

    def test_pr_summary_preserves_collaborator_notes_and_refreshes_scope(self):
        context = {
            "issue": {"number": 1},
            "approval": {"spec": "a" * 64},
            "completion_plan": {"enabled": True, "targets": [{"number": 1}]},
        }
        first = completion.pull_body(context)
        context["completion_plan"]["targets"].append({"number": 3})
        updated = completion.pull_body(context, "Before\n\n" + first + "\n\nAfter")
        self.assertTrue(updated.startswith("Before\n\n"))
        self.assertTrue(updated.endswith("\n\nAfter"))
        self.assertIn("- #3", updated)
        self.assertEqual(updated.count(completion.PR_START), 1)
        with self.assertRaises(Blocked):
            completion.pull_body(context, first + completion.PR_START)
