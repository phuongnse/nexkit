"""Standalone work: simulated GitHub/agent boundaries, with real project commands."""

import io
import os
import sys
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import yaml

from nexkit import approvals, invocations, steps, tasks
from nexkit.ci import main
from nexkit.common import Blocked, canonical, read_json, write_json
from nexkit.delivery import failed, prepare
from nexkit.pipelines import effective_config
from nexkit.policy import config, now
from tests.test_approvals import ApprovalGitHub
from tests.test_invocations import report


class TaskGitHub(ApprovalGitHub):
    def __init__(self, *, agents=False, gate=False, native=False):
        super().__init__(protects=["complete"])
        self.native_jobs = []
        self.job_queries = []
        pipeline = self.cfg["pipelines"]["maintenance"]
        pipeline["entrypoints"] = {"tasks": ".github/workflows/changes.yml"}
        pipeline["steps"] = {
            "inspect-files": {
                "kind": "workflow" if native else "command",
                "subject": "stage",
                "timeout_seconds": 30,
                "retry": "safe",
                **({"job_name": "Project inspection"} if native else {"argv": ["true"]}),
            }
        }
        self.cfg["pipelines"].pop("audit")
        defaults = self.cfg["defaults"]
        for key in ("checks", "release", "merge_method", "application", "clarification"):
            defaults.pop(key, None)
        if agents:
            pipeline["invocations"] = {"inspect": pipeline["invocations"]["inspect"]}
        else:
            pipeline.pop("invocations")
            pipeline["agent_workflows"] = []
            defaults.pop("engine")
            defaults["limits"].pop("agent_calls")
        if not gate:
            pipeline.pop("approvals")

    def api(self, path, method="GET", data=None, **kwargs):
        if path.endswith("/jobs?per_page=100"):
            self.job_queries.append((path, kwargs))
            return deepcopy(self.native_jobs)
        return super().api(path, method, data, **kwargs)


def command_report(context, *, status="done", summary="Checked the project files."):
    return steps.envelope(
        context,
        {"status": status, "summary": summary, "data": {"files": 3}},
        {"kind": "command", "commands": [{"exit_code": 0, "timed_out": False}]},
    )


class TaskCase(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(
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
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.gh = TaskGitHub()

    def start(self, run="100.1"):
        value = prepare(
            self.gh,
            1,
            run,
            "a" * 40,
            pipeline="maintenance",
            individual_agents=True,
            operation="tasks",
        )
        self.assertTrue(value["ready"], value)
        return value

    def step(self, context):
        reserved = steps.prepare(self.gh, context, "inspect-files")
        result = command_report(reserved)
        steps.record(self.gh, reserved, result)
        return reserved, result

    def approve_stage(self):
        checkpoint = self.gh.state["stage_approvals"]["owner-check"]
        stamp = now()
        comment = {
            "id": 2000,
            "body": f"/nexkit approve-stage owner-check {checkpoint['digest']}",
            "created_at": stamp,
            "updated_at": stamp,
            "user": {"login": "owner", "type": "User"},
        }
        self.gh.discussion.append(comment)
        return comment

    def resume(self, run="200.1"):
        os.environ.update(
            GITHUB_WORKFLOW_REF="owner/project/.github/workflows/continue.yml@refs/heads/main",
            GITHUB_RUN_ID=run.split(".")[0],
            GITHUB_RUN_ATTEMPT=run.split(".")[1],
        )
        return approvals.resume(self.gh, 1, "maintenance", "owner-check", "a" * 40, run)


class TaskTests(TaskCase):
    def test_command_only_completion_needs_no_model_or_pr(self):
        with patch.object(self.gh, "strict_protection", side_effect=AssertionError("No PR")):
            context = self.start()
        self.assertNotIn("engine", context["config"])
        self.assertNotIn("agent_calls", context["config"]["limits"])
        self.assertNotIn("candidate", context)
        reserved, result = self.step(context)
        self.assertTrue(steps.record(self.gh, reserved, result)["duplicate"])
        state = tasks.finish(self.gh, context, jobs_succeeded=True)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["agent_calls"], 0)
        self.assertEqual(self.gh.work["state"], "open")
        self.assertIsNone(self.gh.pr)
        self.assertIn("Checked the project files", self.gh.messages[-1])
        before = self.gh.get_state(1), deepcopy(self.gh.messages)
        tasks.finish(self.gh, context, jobs_succeeded=True)
        duplicate = prepare(
            self.gh,
            1,
            "101.1",
            "a" * 40,
            pipeline="maintenance",
            individual_agents=True,
            operation="tasks",
        )
        self.assertFalse(duplicate["ready"])
        self.assertEqual((self.gh.get_state(1), self.gh.messages), before)

    def test_standalone_agent_records_inputs_without_delivery_roles(self):
        self.gh = TaskGitHub(agents=True)
        context = self.start()
        _, output = self.step(context)
        call = invocations.prepare(self.gh, context, "inspect", [output])
        self.assertEqual(call["previous_outputs"], [output])
        result = report(call)
        invocations.record(self.gh, call, result)
        state = tasks.finish(self.gh, context, jobs_succeeded=True)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["agent_calls"], 1)
        self.assertEqual(
            {item["name"] for item in state["task_result"]}, {"inspect", "inspect-files"}
        )

    def test_required_agent_and_step_cannot_be_hidden_by_native_success(self):
        for agents in (False, True):
            with self.subTest(agents=agents):
                self.gh = TaskGitHub(agents=agents)
                context = self.start()
                if agents:
                    self.step(context)
                state = tasks.finish(self.gh, context, jobs_succeeded=True)
                self.assertNotEqual(state["status"], "completed")
                self.assertIn("has not completed", state["reason"])

    def test_optional_omitted_step_allowed_but_started_blocked_step_is_not(self):
        self.gh.cfg["pipelines"]["maintenance"]["steps"]["extra"] = {
            "kind": "command",
            "subject": "stage",
            "timeout_seconds": 30,
            "retry": "safe",
            "required": False,
            "argv": ["true"],
        }
        context = self.start()
        self.step(context)
        steps.require_results(self.gh.state, context)
        optional = steps.prepare(self.gh, context, "extra")
        with self.assertRaisesRegex(Blocked, "started project steps"):
            steps.require_results(self.gh.state, context)
        steps.record(self.gh, optional, command_report(optional, status="blocked"))
        self.assertNotEqual(
            tasks.finish(self.gh, context, jobs_succeeded=True)["status"], "completed"
        )

    def test_skipped_native_job_cannot_complete_successful_managed_steps(self):
        context = self.start()
        self.step(context)
        state = tasks.finish(self.gh, context, jobs_succeeded=False)
        self.assertNotEqual(state["status"], "completed")
        self.assertIn("failed or was skipped", state["reason"])

    def test_duplicate_reservation_and_modified_results_are_rejected(self):
        context = self.start()
        reserved, output = self.step(context)
        with self.assertRaisesRegex(Blocked, "already has a reservation"):
            steps.prepare(self.gh, context, "inspect-files")
        output["result"]["summary"] = "Substituted report"
        with self.assertRaisesRegex(Blocked, "changed after recording"):
            steps.record(self.gh, reserved, output)
        with self.assertRaisesRegex(Blocked, "not a recorded"):
            steps.recorded_inputs(self.gh.state, context, [output])

    def test_input_receipts_reject_duplicates_cross_run_and_ambiguous_kind(self):
        context = self.start()
        _, output = self.step(context)
        for inputs in (
            [output, output],
            [{**output, "run_key": "99.1"}],
            [{**output, "invocation": {"id": "inspect-files"}}],
        ):
            with self.subTest(inputs=inputs), self.assertRaises(Blocked):
                steps.recorded_inputs(self.gh.state, context, inputs)

    def test_changed_context_source_configuration_or_approval_blocks_step(self):
        for change in ("context", "source", "configuration", "approval"):
            with self.subTest(change=change):
                self.gh = TaskGitHub()
                context = self.start()
                reserved = steps.prepare(self.gh, context, "inspect-files")
                if change == "context":
                    reserved["step"]["definition"]["argv"] = ["false"]
                elif change == "source":
                    self.gh.branches["main"] = "e" * 40
                elif change == "configuration":
                    self.gh.cfg["defaults"]["limits"]["minutes"] = 30
                else:
                    self.gh.discussion.clear()
                with self.assertRaises(Blocked):
                    steps.guard(self.gh, reserved)

    def test_cas_conflicts_retry_reservation_and_recording(self):
        context = self.start()
        original = self.gh.save_state
        for operation in ("reserve", "record"):
            calls = 0

            def conflict(number, state, revision):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise Blocked("HTTP 409")
                return original(number, state, revision)

            with patch.object(self.gh, "save_state", side_effect=conflict):
                if operation == "reserve":
                    reserved = steps.prepare(self.gh, context, "inspect-files")
                else:
                    steps.record(self.gh, reserved, command_report(reserved))
            self.assertEqual(calls, 2)
        self.assertEqual(len(self.gh.state["steps"]), 1)

    def test_never_replays_reserved_nonrepeatable_work(self):
        self.gh.cfg["pipelines"]["maintenance"]["steps"]["inspect-files"]["retry"] = "never"
        context = self.start()
        steps.prepare(self.gh, context, "inspect-files")
        state = failed(self.gh, context, "Command interrupted")
        self.assertEqual(state["status"], "blocked")
        self.assertFalse(self.gh.dispatches)
        later = prepare(
            self.gh,
            1,
            "101.1",
            "a" * 40,
            pipeline="maintenance",
            individual_agents=True,
            operation="tasks",
        )
        self.assertFalse(later["ready"])
        self.assertIn("forbids replay", later["reason"])
        self.assertEqual(self.gh.state["attempts"], 1)

    def test_safe_command_retry_preserves_round_time_and_feedback(self):
        context = self.start()
        start_time = self.gh.state["started_at"]
        reserved = steps.prepare(self.gh, context, "inspect-files")
        steps.record(self.gh, reserved, command_report(reserved, status="blocked"))
        self.assertEqual(tasks.finish(self.gh, context, jobs_succeeded=True)["status"], "retry")
        self.assertEqual(self.gh.dispatches[-1][0], "changes.yml")
        later = self.start("101.1")
        self.assertEqual(self.gh.state["started_at"], start_time)
        self.assertEqual(self.gh.state["attempts"], 2)
        self.assertIn("blocked", later["feedback"]["reason"])
        self.step(later)
        self.assertEqual(tasks.finish(self.gh, later, jobs_succeeded=True)["status"], "completed")

    def test_optional_agent_can_be_skipped_when_budget_is_spent(self):
        self.gh = TaskGitHub(agents=True)
        self.gh.cfg["pipelines"]["maintenance"]["invocations"]["inspect"]["required"] = False
        self.gh.state["agent_calls"] = self.gh.cfg["defaults"]["limits"]["agent_calls"]
        context = self.start()
        with self.assertRaisesRegex(Blocked, "budget"):
            invocations.prepare(self.gh, context, "inspect")
        self.step(context)
        self.assertEqual(tasks.finish(self.gh, context, jobs_succeeded=True)["status"], "completed")

    def test_issue_approval_resumes_same_results_without_more_work(self):
        self.gh = TaskGitHub(gate=True)
        context = self.start()
        _, output = self.step(context)
        requested = approvals.request(self.gh, context, "owner-check", inputs=[output])
        self.assertEqual(requested["status"], "waiting_for_approval")
        before = deepcopy(self.gh.state)
        self.assertEqual(tasks.finish(self.gh, context, jobs_succeeded=True), before)
        self.assertIn("Checked the project files", self.gh.messages[-1])
        self.approve_stage()
        resumed = self.resume()
        self.assertTrue(resumed["ready"], resumed)
        state = tasks.finish(self.gh, resumed["context"], jobs_succeeded=True)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["steps"], before["steps"])
        self.assertEqual(state["agent_calls"], 0)
        self.assertEqual(state["attempts"], 1)
        self.assertIn("/runs/200", self.gh.messages[-1])
        self.assertIn("/runs/100", self.gh.messages[-1])

    def test_completion_approval_requires_all_reports_and_rejects_new_results(self):
        self.gh = TaskGitHub(gate=True)
        pipeline = self.gh.cfg["pipelines"]["maintenance"]
        pipeline["steps"]["optional"] = {**pipeline["steps"]["inspect-files"], "required": False}
        context = self.start()
        _, output = self.step(context)
        with self.assertRaisesRegex(Blocked, "every completed"):
            approvals.request(self.gh, context, "owner-check")
        approvals.request(self.gh, context, "owner-check", inputs=[output])
        self.approve_stage()
        resumed = self.resume()["context"]
        added = steps.prepare(self.gh, resumed, "optional")
        steps.record(self.gh, added, command_report(added))
        state = tasks.finish(self.gh, resumed, jobs_succeeded=True)
        self.assertNotEqual(state["status"], "completed")
        self.assertIn("every completed", state["reason"])

    def test_approval_revocation_blocks_completion_without_new_call(self):
        self.gh = TaskGitHub(gate=True)
        context = self.start()
        _, output = self.step(context)
        approvals.request(self.gh, context, "owner-check", inputs=[output])
        decision = self.approve_stage()
        resumed = self.resume()["context"]
        decision["body"] = "Revoked"
        state = tasks.finish(self.gh, resumed, jobs_succeeded=True)
        self.assertEqual(state["status"], "blocked")
        self.assertEqual(state["agent_calls"], 0)
        self.assertFalse(self.gh.dispatches)

    def test_protected_step_requires_gate_and_claim_cannot_replay_started_step(self):
        self.gh = TaskGitHub(gate=True)
        self.gh.cfg["pipelines"]["maintenance"]["approvals"]["owner-check"]["protects"] = [
            "step:inspect-files"
        ]
        context = self.start()
        with self.assertRaises(approvals.ApprovalRequired):
            steps.prepare(self.gh, context, "inspect-files")
        approvals.request(self.gh, context, "owner-check")
        self.approve_stage()
        resumed = self.resume()["context"]
        steps.prepare(self.gh, resumed, "inspect-files")
        self.assertFalse(self.resume("201.1")["ready"])
        self.assertEqual(len(self.gh.state["steps"]), 1)

    def test_step_deadline_is_enforced(self):
        context = self.start()
        old = (datetime.fromisoformat(now()) - timedelta(minutes=6)).isoformat()
        with patch("nexkit.steps.now", return_value=old):
            reserved = steps.prepare(self.gh, context, "inspect-files")
        with self.assertRaisesRegex(Blocked, "deadline"):
            steps.guard(self.gh, reserved)

    def test_cli_preparation_record_and_finalization_use_tasks_route(self):
        def invoke(arguments):
            with patch("sys.argv", ["ci", *arguments]):
                return main()

        context = self.start()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("nexkit.ci.GitHub", return_value=self.gh),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            root = Path(directory)
            write_json(root / "context.json", context)
            self.assertEqual(
                invoke(
                    [
                        "prepare-step",
                        "--context",
                        str(root / "context.json"),
                        "--step",
                        "inspect-files",
                        "--kit-ref",
                        "a" * 40,
                        "--out",
                        str(root / "step.json"),
                    ]
                ),
                0,
            )
            reserved = read_json(root / "step.json")
            write_json(root / "result.json", command_report(reserved))
            self.assertEqual(
                invoke(
                    [
                        "record-step",
                        "--context",
                        str(root / "step.json"),
                        "--result",
                        str(root / "result.json"),
                        "--kit-ref",
                        "a" * 40,
                        "--out",
                        str(root / "recorded.json"),
                    ]
                ),
                0,
            )
            self.assertEqual(
                invoke(
                    [
                        "finish-tasks",
                        "--context",
                        str(root / "context.json"),
                        "--kit-ref",
                        "a" * 40,
                        "--jobs-succeeded",
                        "--out",
                        str(root / "final.json"),
                    ]
                ),
                0,
            )
            self.assertEqual(read_json(root / "final.json")["status"], "completed")


class TaskPublicationTests(TaskCase):
    def completion_messages(self):
        return [body for body in self.gh.messages if "**Work completed" in body]

    def test_completed_state_with_copied_summaries_can_still_publish_once(self):
        state = {
            "run_key": "100.1",
            "task_result": [{"name": "inspect", "summary": "Recorded findings.\n\nFinal limit."}],
        }
        tasks.notice(self.gh, 1, state)
        tasks.notice(self.gh, 1, state)
        self.assertEqual(len(self.completion_messages()), 1)
        self.assertIn(state["task_result"][0]["summary"], self.completion_messages()[0])

    def test_full_agent_and_step_summaries_survive_recording_and_publication(self):
        self.gh = TaskGitHub(agents=True)
        context = self.start()
        content = (
            "## Findings\n\n"
            "- [Reference](https://example.com/guide)\n"
            "- Tiếng Việt, 日本語 and 🧪\n\n"
            "```python\nprint('checked')\n```\n\n"
        )
        agent_summary = content + "a" * (3521 - len(content) - len("\n\nFinal findings."))
        agent_summary += "\n\nFinal findings."
        step_summary = content + "b" * 4000 + "\n\nRemaining limitations."
        reserved = steps.prepare(self.gh, context, "inspect-files")
        step_output = command_report(reserved, summary=step_summary)
        steps.record(self.gh, reserved, step_output)
        call = invocations.prepare(self.gh, context, "inspect", [step_output])
        agent_output = report(call)
        agent_output["result"]["summary"] = agent_summary
        invocations.record(self.gh, call, agent_output)
        self.assertTrue(invocations.record(self.gh, call, agent_output)["duplicate"])
        self.assertTrue(steps.record(self.gh, reserved, step_output)["duplicate"])
        state = tasks.finish(self.gh, context, jobs_succeeded=True)
        self.assertEqual(state["status"], "completed")
        for group, name, summary in (
            ("invocations", "inspect", agent_summary),
            ("steps", "inspect-files", step_summary),
        ):
            with self.subTest(group=group):
                self.assertEqual(state[group]["100.1/" + name]["summary"], summary)
                self.assertIn(summary, self.completion_messages()[0])
        self.assertNotIn("...", self.completion_messages()[0])
        self.assertIn("/actions/runs/100", self.completion_messages()[0])
        self.assertIn("Result artifacts:", self.completion_messages()[0])

    def test_every_completed_result_is_published_beyond_twenty(self):
        pipeline = self.gh.cfg["pipelines"]["maintenance"]
        definition = pipeline["steps"].pop("inspect-files")
        names = [f"inspect-{index}" for index in range(25)]
        pipeline["steps"] = {name: deepcopy(definition) for name in names}
        context = self.start()
        summaries = {}
        for name in names:
            reserved = steps.prepare(self.gh, context, name)
            summaries[name] = f"Result {name}.\n\n" + "Checked data. " * 280 + f"\n\nEnd {name}."
            steps.record(self.gh, reserved, command_report(reserved, summary=summaries[name]))
        state = tasks.finish(self.gh, context, jobs_succeeded=True)
        self.assertEqual(len(state["task_result"]), len(names))
        self.assertGreater(len(self.completion_messages()), 1)
        published = "".join(body.split("\n\n", 1)[1] for body in self.completion_messages())
        for summary in summaries.values():
            self.assertIn(summary, published)
        self.assertNotIn("more results", published)

    def test_oversized_unicode_result_is_split_in_order_without_content_loss(self):
        self.gh = TaskGitHub(agents=True)
        context = self.start()
        self.step(context)
        call = invocations.prepare(self.gh, context, "inspect")
        output = report(call)
        paragraphs = [f"Paragraph {index}: " + "Tiếng Việt 日本語 🧪 " * 120 for index in range(60)]
        summary = "\n\n".join(paragraphs) + "\n\nFinal limitations."
        output["result"]["summary"] = summary
        invocations.record(self.gh, call, output)
        state = tasks.finish(self.gh, context, jobs_succeeded=True)
        self.assertEqual(state["status"], "completed")
        comments = self.completion_messages()
        self.assertGreater(len(comments), 1)
        for index, body in enumerate(comments, 1):
            self.assertLessEqual(len(body.encode("utf-8")), 60000)
            self.assertIn(f"part {index} of {len(comments)}", body)
        published = "".join(body.split("\n\n", 1)[1] for body in comments)
        self.assertIn(summary, published)
        self.assertIn("Result artifacts:", published)
        self.assertIn("/actions/runs/100", published)
        before = deepcopy(self.gh.messages)
        tasks.finish(self.gh, context, jobs_succeeded=True)
        self.assertEqual(self.gh.messages, before)

    def test_unicode_without_whitespace_is_split_only_between_complete_characters(self):
        self.gh = TaskGitHub(agents=True)
        context = self.start()
        self.step(context)
        call = invocations.prepare(self.gh, context, "inspect")
        output = report(call)
        summary = "🧪日本語" * 13000
        output["result"]["summary"] = summary
        invocations.record(self.gh, call, output)
        tasks.finish(self.gh, context, jobs_succeeded=True)
        comments = self.completion_messages()
        self.assertGreater(len(comments), 1)
        self.assertIn(summary, "".join(body.split("\n\n", 1)[1] for body in comments))
        for body in comments:
            self.assertLessEqual(len(body.encode()), 60000)
            self.assertNotIn("\ufffd", body)

    def test_code_fences_are_reopened_when_a_code_block_spans_comments(self):
        for prefix in ("", "> ", "> > "):
            with self.subTest(prefix=prefix):
                self.gh = TaskGitHub(agents=True)
                context = self.start()
                self.step(context)
                call = invocations.prepare(self.gh, context, "inspect")
                output = report(call)
                code = [
                    prefix + f"print('Line {index}: " + "x" * 100 + "')\n" for index in range(1100)
                ]
                opening = prefix + "```python\n"
                output["result"]["summary"] = (
                    opening + "".join(code) + prefix + "```\n\nFinal finding."
                )
                invocations.record(self.gh, call, output)
                tasks.finish(self.gh, context, jobs_succeeded=True)
                comments = self.completion_messages()
                self.assertGreater(len(comments), 1)
                published = "".join(body.split("\n\n", 1)[1] for body in comments)
                for line in code:
                    self.assertEqual(published.count(line), 1)
                for body in comments[1:]:
                    self.assertTrue(body.split("\n\n", 1)[1].startswith(opening))
                self.assertIn("```\n\nFinal finding.", published)
                self.assertIn("Result artifacts:", published)

    def test_reference_links_keep_their_definitions_in_each_comment_that_uses_them(self):
        self.gh = TaskGitHub(agents=True)
        context = self.start()
        self.step(context)
        call = invocations.prepare(self.gh, context, "inspect")
        output = report(call)
        definition = '[doc]: https://example.com/evidence "Evidence"'
        output["result"]["summary"] = (
            "[Evidence][doc]\n\n" + "Findings and verification.\n\n" * 3000 + definition
        )
        invocations.record(self.gh, call, output)
        tasks.finish(self.gh, context, jobs_succeeded=True)
        comments = self.completion_messages()
        self.assertGreater(len(comments), 1)
        self.assertIn("[Evidence][doc]", comments[0])
        self.assertIn(definition, comments[0])
        self.assertEqual("".join(comments).count("Findings and verification."), 3000)
        for body in comments:
            self.assertLessEqual(len(body.encode()), 60000)

    def test_reference_definition_larger_than_a_comment_still_publishes_all_text(self):
        self.gh = TaskGitHub(agents=True)
        context = self.start()
        self.step(context)
        call = invocations.prepare(self.gh, context, "inspect")
        output = report(call)
        summary = "[a]\n\n[a]: https://example.com/" + "x" * 61000
        output["result"]["summary"] = summary
        invocations.record(self.gh, call, output)
        tasks.finish(self.gh, context, jobs_succeeded=True)
        comments = self.completion_messages()
        self.assertGreater(len(comments), 1)
        self.assertIn(summary, "".join(body.split("\n\n", 1)[1] for body in comments))
        for body in comments:
            self.assertLessEqual(len(body.encode()), 60000)

    def test_a_link_crossing_a_size_boundary_stays_in_one_comment(self):
        self.gh = TaskGitHub(agents=True)
        context = self.start()
        self.step(context)
        call = invocations.prepare(self.gh, context, "inspect")
        output = report(call)
        link = "[the complete reference link](https://example.com/" + "y" * 1000 + ")"
        output["result"]["summary"] = "x" * 59400 + " See " + link + " for details."
        invocations.record(self.gh, call, output)
        tasks.finish(self.gh, context, jobs_succeeded=True)
        self.assertTrue(any(link in body for body in self.completion_messages()))

    def test_conflicting_reference_labels_keep_each_results_original_target(self):
        self.gh = TaskGitHub(agents=True)
        context = self.start()
        step = steps.prepare(self.gh, context, "inspect-files")
        step_summary = "[Step evidence][doc]\n\n[doc]: https://example.com/step"
        steps.record(self.gh, step, command_report(step, summary=step_summary))
        call = invocations.prepare(self.gh, context, "inspect")
        output = report(call)
        agent_summary = "[Agent evidence][doc]\n\n[doc]: https://example.com/agent"
        output["result"]["summary"] = agent_summary
        invocations.record(self.gh, call, output)
        tasks.finish(self.gh, context, jobs_succeeded=True)
        comments = self.completion_messages()
        self.assertEqual(len(comments), 2)
        self.assertIn(agent_summary, comments[0])
        self.assertNotIn("https://example.com/step", comments[0])
        self.assertIn(step_summary, comments[1])
        self.assertNotIn("https://example.com/agent", comments[1])
        for body in comments:
            self.assertIn("Result artifacts:", body)

    def test_completion_approval_restores_large_reports_without_duplicate_text(self):
        self.gh = TaskGitHub(agents=True, gate=True)
        context = self.start()
        _, step_output = self.step(context)
        call = invocations.prepare(self.gh, context, "inspect")
        output = report(call)
        summary = "x" * 380000 + "\n\nFull approved report."
        output["result"]["summary"] = summary
        invocations.record(self.gh, call, output)
        requested = approvals.request(self.gh, context, "owner-check", inputs=[step_output, output])
        self.assertEqual(requested["status"], "waiting_for_approval")
        self.assertLess(len(canonical(self.gh.state).encode()), 750000)
        self.approve_stage()
        resumed = self.resume()["context"]
        self.assertEqual(
            approvals.restored_data(self.gh.state, resumed)["inputs"], [step_output, output]
        )
        state = tasks.finish(self.gh, resumed, jobs_succeeded=True)
        self.assertEqual(state["status"], "completed")
        self.assertIn(
            summary, "".join(body.split("\n\n", 1)[1] for body in self.completion_messages())
        )
        altered = deepcopy(state)
        altered["invocations"]["100.1/inspect"]["summary"] = "Substituted report"
        with self.assertRaisesRegex(Blocked, "summary content changed"):
            approvals.restored_data(altered, resumed)

    def test_rerunning_approval_continuation_recovers_only_missing_comments(self):
        self.gh = TaskGitHub(agents=True, gate=True)
        context = self.start()
        _, step_output = self.step(context)
        call = invocations.prepare(self.gh, context, "inspect")
        output = report(call)
        output["result"]["summary"] = "x" * 130000 + "\n\nFinal finding."
        invocations.record(self.gh, call, output)
        approvals.request(self.gh, context, "owner-check", inputs=[step_output, output])
        self.approve_stage()
        resumed = self.resume()["context"]
        original = self.gh.comment
        posted = 0

        def interrupted(number, body):
            nonlocal posted
            original(number, body)
            posted += 1
            if posted == 2:
                raise Blocked("Publication interrupted")

        with patch.object(self.gh, "comment", side_effect=interrupted):
            with self.assertRaisesRegex(Blocked, "Publication interrupted"):
                tasks.finish(self.gh, resumed, jobs_succeeded=True)
        before = self.gh.get_state(1)
        os.environ["GITHUB_RUN_ATTEMPT"] = "2"
        recovered = self.resume("200.2")
        self.assertFalse(recovered["ready"])
        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(len(self.completion_messages()), 3)
        self.assertEqual(self.gh.get_state(1), before)
        self.resume("200.2")
        self.assertEqual(len(self.completion_messages()), 3)
        for change in ("pipeline", "kit", "caller"):
            with self.subTest(change=change), patch.dict(os.environ):
                if change == "caller":
                    os.environ["GITHUB_WORKFLOW_REF"] = (
                        "owner/project/.github/workflows/changes.yml@refs/heads/main"
                    )
                with self.assertRaises(Blocked):
                    approvals.resume(
                        self.gh,
                        1,
                        "wrong" if change == "pipeline" else "maintenance",
                        "owner-check",
                        "f" * 40 if change == "kit" else "a" * 40,
                        "200.2",
                    )

    def test_completion_does_not_duplicate_large_summaries_in_bounded_state(self):
        self.gh = TaskGitHub(agents=True)
        context = self.start()
        self.step(context)
        call = invocations.prepare(self.gh, context, "inspect")
        output = report(call)
        summary = "Large complete result.\n\n" + "x" * 450000 + "\n\nFinal limitation."
        output["result"]["summary"] = summary
        original = self.gh.save_state

        def bounded_save(number, state, revision):
            if len(canonical(state).encode()) > 900000:
                raise Blocked("Delivery state exceeds the bounded GitHub state size")
            return original(number, state, revision)

        with patch.object(self.gh, "save_state", side_effect=bounded_save):
            invocations.record(self.gh, call, output)
            state = tasks.finish(self.gh, context, jobs_succeeded=True)
        self.assertEqual(state["status"], "completed")
        self.assertLessEqual(len(canonical(state).encode()), 900000)
        self.assertIn(
            summary,
            "".join(body.split("\n\n", 1)[1] for body in self.completion_messages()),
        )

    def test_a_copied_completion_body_from_a_user_cannot_suppress_publication(self):
        context = self.start()
        self.step(context)

        def copied_comment(number, body):
            self.gh.discussion.append({"body": body, "user": {"login": "owner", "type": "User"}})
            raise Blocked("Publication interrupted")

        with patch.object(self.gh, "comment", side_effect=copied_comment):
            with self.assertRaisesRegex(Blocked, "Publication interrupted"):
                tasks.finish(self.gh, context, jobs_succeeded=True)
        tasks.finish(self.gh, context, jobs_succeeded=True)
        self.assertEqual(len(self.completion_messages()), 1)
        self.assertIn("Checked the project files.", self.completion_messages()[0])

    def test_partial_publication_resumes_after_accepted_comment_and_lost_response(self):
        self.gh = TaskGitHub(agents=True)
        context = self.start()
        self.step(context)
        call = invocations.prepare(self.gh, context, "inspect")
        output = report(call)
        summary = "x" * 130000 + "\n\nComplete final finding."
        output["result"]["summary"] = summary
        invocations.record(self.gh, call, output)
        original = self.gh.comment
        posted = 0

        def lose_response(number, body):
            nonlocal posted
            original(number, body)
            posted += 1
            if posted == 2:
                raise Blocked("GitHub response interrupted after accepting the comment")

        with patch.object(self.gh, "comment", side_effect=lose_response):
            with self.assertRaisesRegex(Blocked, "response interrupted"):
                tasks.finish(self.gh, context, jobs_succeeded=True)
        self.assertEqual(self.gh.state["status"], "completed")
        self.assertEqual(len(self.completion_messages()), 2)
        before = self.gh.get_state(1)
        tasks.finish(self.gh, context, jobs_succeeded=True)
        comments = self.completion_messages()
        self.assertEqual(len(comments), 3)
        self.assertEqual(len(set(comments)), 3)
        self.assertIn(summary, "".join(body.split("\n\n", 1)[1] for body in comments))
        tasks.finish(self.gh, context, jobs_succeeded=True)
        self.assertEqual(self.completion_messages(), comments)
        self.assertEqual(self.gh.get_state(1), before)

    def test_failed_finalizer_job_rerun_recovers_publication_without_replaying_work(self):
        self.gh = TaskGitHub(agents=True)
        context = self.start()
        self.step(context)
        call = invocations.prepare(self.gh, context, "inspect")
        output = report(call)
        output["result"]["summary"] = "x" * 130000 + "\n\nFinal finding."
        invocations.record(self.gh, call, output)
        original = self.gh.comment

        def interrupted(number, body):
            original(number, body)
            raise Blocked("Publication response lost")

        with (
            tempfile.TemporaryDirectory() as directory,
            patch("nexkit.ci.GitHub", return_value=self.gh),
            patch("sys.stdout", new_callable=io.StringIO),
            patch("sys.stderr", new_callable=io.StringIO),
        ):
            root = Path(directory)
            write_json(root / "context.json", context)
            arguments = [
                "ci",
                "finish-tasks",
                "--context",
                str(root / "context.json"),
                "--kit-ref",
                "a" * 40,
                "--jobs-succeeded",
                "--out",
                str(root / "outcome.json"),
            ]
            with (
                patch("sys.argv", arguments),
                patch.object(self.gh, "comment", side_effect=interrupted),
            ):
                self.assertEqual(main(), 2)
            self.assertEqual(len(self.completion_messages()), 1)
            before = self.gh.get_state(1)
            os.environ["GITHUB_RUN_ATTEMPT"] = "2"
            with patch("sys.argv", arguments):
                self.assertEqual(main(), 0)
            self.assertEqual(len(self.completion_messages()), 3)
            self.assertEqual(self.gh.get_state(1), before)
            self.assertEqual(read_json(root / "outcome.json")["status"], "completed")
            with self.assertRaisesRegex(Blocked, "another run attempt"):
                invocations.runtime_guard(self.gh, context, "a" * 40)
            os.environ["GITHUB_RUN_ID"] = "101"
            with patch("sys.argv", arguments):
                self.assertEqual(main(), 2)
            self.assertEqual(len(self.completion_messages()), 3)


class ProjectCommandTests(unittest.TestCase):
    def context(self, argv, setup=()):
        cfg = effective_config(TaskGitHub().cfg, "maintenance")
        cfg["environment"]["setup"] = list(setup)
        return {
            "run_key": "100.1",
            "base": "b" * 40,
            "source": "b" * 40,
            "issue": {"number": 1, "title": "Inspect files"},
            "config": cfg,
            "previous_outputs": [{"result": {"summary": "Previous check"}}],
            "step": {
                "id": "inspect-files",
                "definition": {
                    "kind": "command",
                    "argv": argv,
                    "timeout_seconds": 5,
                },
            },
        }

    def execute(self, argv, *, setup=()):
        with tempfile.TemporaryDirectory() as root:
            return steps.execute(self.context(argv, setup), root)

    def test_real_command_receives_inputs_and_keeps_setup_home(self):
        setup = [
            sys.executable,
            "-c",
            "from pathlib import Path; Path.home().joinpath('ready').write_text('yes')",
        ]
        script = """import json
from pathlib import Path
assert Path.home().joinpath('ready').read_text() == 'yes'
data = json.loads(Path('.nexkit-step-input.json').read_text())
assert data['inputs'][0]['result']['summary'] == 'Previous check'
Path('.nexkit-step-result.json').write_text(json.dumps({'status':'done','summary':'Checked actual inputs','data':{'files':3}}))
"""
        value = self.execute([sys.executable, "-c", script], setup=[setup])
        self.assertEqual(value["result"]["data"], {"files": 3})
        self.assertEqual(len(value["evidence"]["commands"]), 2)

    def test_failed_or_timed_out_real_command_cannot_report_success(self):
        value = self.execute([sys.executable, "-c", "raise SystemExit(3)"])
        self.assertEqual(value["result"]["status"], "blocked")
        self.assertEqual(value["evidence"]["commands"][0]["exit_code"], 3)
        context = self.context([sys.executable, "-c", "import time; time.sleep(2)"])
        context["step"]["definition"]["timeout_seconds"] = 0.1
        with tempfile.TemporaryDirectory() as root:
            value = steps.execute(context, root)
        self.assertTrue(value["evidence"]["commands"][0]["timed_out"])
        self.assertEqual(value["result"]["status"], "blocked")

    def test_setup_failure_stops_before_project_command(self):
        value = self.execute(
            [sys.executable, "-c", "raise AssertionError('Must not run')"],
            setup=[[sys.executable, "-c", "raise SystemExit(2)"]],
        )
        self.assertEqual(len(value["evidence"]["commands"]), 1)
        self.assertEqual(value["result"]["status"], "blocked")

    def test_setup_cannot_prepopulate_step_output(self):
        with self.assertRaisesRegex(Blocked, "setup cannot supply"):
            self.execute(
                ["true"],
                setup=[
                    [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; Path('.nexkit-step-result.json').write_text('{}')",
                    ]
                ],
            )

    def test_fake_success_after_failure_is_rejected(self):
        with self.assertRaisesRegex(Blocked, "failed command cannot report done"):
            self.execute(
                [
                    sys.executable,
                    "-c",
                    'from pathlib import Path; Path(\'.nexkit-step-result.json\').write_text(\'{"status":"done","summary":"False success"}\'); raise SystemExit(1)',
                ]
            )

    def test_reserved_paths_and_result_symlinks_are_rejected(self):
        context = self.context(["true"])
        with tempfile.TemporaryDirectory() as root:
            Path(root, ".nexkit-step-input.json").symlink_to("missing")
            with self.assertRaisesRegex(Blocked, "must not exist"):
                steps.execute(context, root)
        with self.assertRaisesRegex(Blocked, "regular JSON"):
            self.execute(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path('.nexkit-step-result.json').symlink_to('missing')",
                ]
            )

    def test_result_contract_rejects_unknown_fields_types_and_oversize(self):
        for value in (
            [],
            {},
            {"status": "done", "summary": ""},
            {"status": {}, "summary": "x"},
            {"status": "done", "summary": "ok", "extra": True},
            {"status": "done", "summary": "x" * 8001},
            {"status": "done", "summary": "ok", "data": "x" * 48000},
        ):
            with self.subTest(value=str(value)[:100]), self.assertRaises(Blocked):
                steps.result_data(value)


class NativeStepTests(TaskCase):
    def native(self):
        self.gh = TaskGitHub(native=True)
        context = self.start()
        reserved = steps.prepare(self.gh, context, "inspect-files")
        stamp = datetime.fromisoformat(reserved["step"]["started_at"])
        self.gh.native_jobs = [
            {
                "id": 500,
                "name": "Project inspection",
                "status": "completed",
                "conclusion": "success",
                "started_at": stamp.isoformat(),
                "completed_at": (stamp + timedelta(seconds=1)).isoformat(),
                "html_url": "https://github.com/owner/project/actions/runs/100/job/500",
            }
        ]
        return context, reserved

    def test_native_success_uses_exact_attempt_job_and_server_conclusion(self):
        context, reserved = self.native()
        output = steps.workflow_report(self.gh, reserved)
        steps.record(self.gh, reserved, output)
        self.assertEqual(
            self.gh.job_queries[-1],
            (
                "repos/owner/project/actions/runs/100/attempts/1/jobs?per_page=100",
                {"pages": True, "collection": "jobs"},
            ),
        )
        self.assertEqual(tasks.finish(self.gh, context, jobs_succeeded=True)["status"], "completed")

    def test_native_wrong_name_duplicate_skipped_stale_or_timeout_are_rejected(self):
        for mode in ("name", "duplicate", "skipped", "stale", "timeout"):
            with self.subTest(mode=mode):
                _, reserved = self.native()
                job = self.gh.native_jobs[0]
                if mode == "name":
                    job["name"] = "Another job"
                elif mode == "duplicate":
                    self.gh.native_jobs.append(deepcopy(job))
                elif mode == "skipped":
                    job["started_at"] = None
                elif mode == "stale":
                    job["started_at"] = "2000-01-01T00:00:00Z"
                else:
                    job["completed_at"] = (
                        datetime.fromisoformat(job["started_at"]) + timedelta(seconds=31)
                    ).isoformat()
                with self.assertRaises(Blocked):
                    steps.workflow_report(self.gh, reserved)

    def test_native_failure_cannot_be_overridden_by_a_success_result(self):
        _, reserved = self.native()
        self.gh.native_jobs[0]["conclusion"] = "failure"
        self.assertEqual(steps.workflow_report(self.gh, reserved)["result"]["status"], "blocked")
        with self.assertRaisesRegex(Blocked, "cannot report done"):
            steps.workflow_report(self.gh, reserved, {"status": "done", "summary": "Fake success"})


class TaskConfigTests(unittest.TestCase):
    def test_invalid_tasks_and_steps_are_rejected_during_setup(self):
        for mode in (
            "mixed",
            "no-work",
            "editor",
            "candidate",
            "retry",
            "timeout",
            "unknown",
            "job-expression",
            "duplicate-job",
            "pr-approval",
        ):
            gh = TaskGitHub(
                agents=mode == "editor",
                gate=mode == "pr-approval",
                native=mode in ("job-expression", "duplicate-job"),
            )
            pipeline = gh.cfg["pipelines"]["maintenance"]
            value = pipeline["steps"]["inspect-files"]
            if mode == "mixed":
                pipeline["entrypoints"]["delivery"] = ".github/workflows/changes.yml"
            elif mode == "no-work":
                pipeline.pop("steps")
            elif mode == "editor":
                pipeline["invocations"]["inspect"]["contract"] = "deliver"
            elif mode == "candidate":
                value["subject"] = "candidate"
            elif mode == "retry":
                value["retry"] = "automatic"
            elif mode == "timeout":
                value["timeout_seconds"] = 31
            elif mode == "unknown":
                value["extra"] = True
            elif mode == "job-expression":
                value["job_name"] = "${{ matrix.name }}"
            elif mode == "duplicate-job":
                pipeline["steps"]["other"] = deepcopy(value)
            else:
                pipeline["approvals"]["owner-check"]["mode"] = "pull_request"
            with self.subTest(mode=mode), self.assertRaises(Blocked):
                config(gh.cfg)

    def test_native_workflows_keep_commands_separate_from_write_credentials(self):
        root = Path(__file__).resolve().parents[1]
        command = yaml.safe_load((root / ".github/workflows/project-step.yml").read_text())
        execute = command["jobs"]["execute"]
        self.assertTrue(all(value == "read" for value in execute["permissions"].values()))
        run = next(step for step in execute["steps"] if "execute-step" in step.get("run", ""))
        self.assertNotIn("GH_TOKEN", run["env"])
        self.assertEqual(run["env"]["NEXKIT_EXEC_USER"], "nexkit-agent")
        self.assertTrue(any("guard-step" in step.get("run", "") for step in execute["steps"]))
        for filename in ("prepare-step.yml", "record-step.yml", "finish-tasks.yml"):
            workflow = yaml.safe_load((root / ".github/workflows" / filename).read_text())
            self.assertEqual(workflow["permissions"], {})
            for job in workflow["jobs"].values():
                self.assertNotIn("self-hosted", job["runs-on"])
