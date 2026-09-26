"""Individual invocation authority with mocked GitHub/CLI and real file operations."""

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import yaml

from nexkit import invocations
from nexkit.artifacts import downloaded, identifiers
from nexkit.checks import combine_checks
from nexkit.ci import collect, load_context, main, materialize, prepare_job
from nexkit.common import Blocked, digest, read_json, write_json
from nexkit.delivery import failed, finish, prepare, publish, revalidate
from nexkit.pipelines import effective_config
from nexkit.policy import config
from nexkit.project import doctor
from nexkit.subscription import cli_command
from nexkit.workspace import restore, snapshot
from tests.support import agent, reviewed, verified
from tests.test_delivery import bundle
from tests.test_pipelines import ComposedGitHub


class InvocationGitHub(ComposedGitHub):
    def __init__(self):
        super().__init__()
        self.cfg["defaults"].pop("models")
        self.cfg["defaults"]["clarification"] = {"agent_minutes": 3}
        definitions = {}
        for name, role in (
            ("inspect", "task"),
            ("edit", "deliver"),
            ("polish", "deliver"),
            ("audit", "review"),
        ):
            path = f".nexkit/controls/{name}.md"
            self.files[path] = (
                f"Perform the accepted {name} task within the approved requirement.\n".encode()
            )
            definitions[name] = {
                "contract": role,
                "model": name + "-model",
                "minutes": 4,
                "task": path,
            }
        definitions["edit"]["reasoning_effort"] = "max"
        definitions["inspect"]["skills"] = [".nexkit/controls/skills/domain-audit/SKILL.md"]
        self.files[".nexkit/controls/skills/domain-audit/SKILL.md"] = (
            b"---\nname: domain-audit\ndescription: Inspect domain assumptions.\n---\nRead references/contracts.md.\n"
        )
        self.files[".nexkit/controls/skills/domain-audit/references/contracts.md"] = (
            b"Inspect actual signed integer behavior.\n"
        )
        self.cfg["pipelines"]["maintenance"]["invocations"] = definitions
        for path, content in self.files.items():
            self.cfg["files"][path] = {
                "sha256": hashlib.sha256(content).hexdigest(),
                "managed": True,
            }


def report(context):
    role = context["invocation"]["role"]
    result = (
        bundle(context)
        if role == "deliver"
        else {
            "run_key": context["run_key"],
            "candidate": context.get("candidate"),
            "result": agent(role),
            "unchanged": True,
            "independent": True,
        }
    )
    result["invocation"] = {"id": context["invocation"]["id"], "context": digest(context)}
    result["result"]["skills_used"].extend(
        Path(path).parent.name for path in context["invocation"]["definition"].get("skills", [])
    )
    return result


def check_reports(context):
    return [
        {
            "candidate": context["candidate"],
            "passed": True,
            "producer": {"run_key": context["run_key"], "check": item["name"]},
            "checks": [item],
        }
        for item in verified(context["candidate"])["checks"]
    ]


class InvocationTests(unittest.TestCase):
    def setUp(self):
        self.gh = InvocationGitHub()
        env = {
            "GITHUB_WORKFLOW_REF": "owner/project/.github/workflows/changes.yml@refs/heads/main",
            "GITHUB_WORKFLOW_SHA": "b" * 40,
            "GITHUB_REPOSITORY": self.gh.repository,
            "GITHUB_RUN_ID": "100",
            "GITHUB_RUN_ATTEMPT": "1",
        }
        self.environment = patch.dict(os.environ, env)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.context = self.start()
        self.assertTrue(self.context["ready"], self.context)

    def start(self, run="100.1"):
        return prepare(self.gh, 1, run, "a" * 40, pipeline="maintenance", individual_agents=True)

    def candidate(self):
        context = invocations.prepare(self.gh, self.context, "edit")
        result = report(context)
        invocations.record(self.gh, context, result)
        return publish(self.gh, context, result)

    def test_native_round_reserves_no_fixed_pair_and_each_call_counts_once(self):
        self.assertEqual(self.gh.state["agent_calls"], 0)
        first = invocations.prepare(self.gh, self.context, "inspect")
        self.assertEqual(self.gh.state["agent_calls"], 1)
        with self.assertRaisesRegex(Blocked, "already reserved"):
            invocations.prepare(self.gh, self.context, "inspect")
        result = report(first)
        invocations.record(self.gh, first, result)
        self.assertTrue(invocations.record(self.gh, first, result)["duplicate"])
        next_call = invocations.prepare(self.gh, self.context, "edit", [result])
        self.assertEqual(next_call["previous_outputs"], [result])
        self.assertEqual(self.gh.state["agent_calls"], 2)

    def test_arbitrary_named_steps_publish_review_and_merge(self):
        context = self.candidate()
        checks = check_reports(context)
        reviewer = invocations.prepare(self.gh, context, "audit", check_reports=checks)
        self.assertEqual(reviewer["verification"], combine_checks(context, checks))
        result = report(reviewer)
        invocations.record(self.gh, reviewer, result)
        outcome = finish(self.gh, context, combine_checks(context, list(reversed(checks))), result)
        self.assertEqual(outcome["status"], "merged")
        self.assertEqual(outcome["agent_calls"], 2)
        self.assertEqual(len(self.gh.merges), 1)

    def test_per_invocation_model_effort_runner_setup_and_deadline(self):
        self.gh.cfg["pipelines"]["maintenance"]["invocations"]["edit"].update(
            agent_runner=["self-hosted", "linux", "x64", "consumer-special"],
            setup=[["python3", "-c", "print('selected setup')"]],
        )
        self.context = self.start("101.1")
        cfg = effective_config(self.gh.cfg, "maintenance")
        self.assertNotIn("models", cfg)
        context = invocations.prepare(self.gh, self.context, "edit")
        effective = invocations.execution_config(context)
        self.assertEqual(effective["models"]["implement"], "edit-model")
        self.assertEqual(effective["reasoning_effort"]["review"], "max")
        self.assertEqual(effective["environment"]["agent_runner"][-1], "consumer-special")
        self.assertIn("selected setup", str(effective["environment"]["setup"]))
        command = cli_command(effective, "deliver", Path("/tmp/test-scratch"))
        self.assertEqual(command[command.index("--model") + 1], "edit-model")
        self.assertIn('model_reasoning_effort="max"', command)
        self.assertEqual(context["agent_minutes"], 4)
        self.assertEqual(context["config"], cfg)
        later = (
            datetime.fromisoformat(context["invocation"]["started_at"]) + timedelta(minutes=10)
        ).isoformat()
        with (
            patch("nexkit.invocations.now", return_value=later),
            self.assertRaisesRegex(Blocked, "deadline"),
        ):
            invocations.guard(self.gh, context)

    def test_duplicate_publication_recovers_without_another_commit(self):
        context = invocations.prepare(self.gh, self.context, "edit")
        result = report(context)
        invocations.record(self.gh, context, result)
        one = publish(self.gh, context, result)
        two = publish(self.gh, context, result)
        self.assertEqual(one, two)
        self.assertEqual(self.gh.commit_sequence, 1)

    def test_multiple_source_steps_serialize_and_invalidate_old_review(self):
        first = invocations.prepare(self.gh, self.context, "edit")
        with self.assertRaisesRegex(Blocked, "source editor"):
            invocations.prepare(self.gh, self.context, "polish")
        result = report(first)
        invocations.record(self.gh, first, result)
        candidate = publish(self.gh, first, result)
        reviewer = invocations.prepare(
            self.gh, candidate, "audit", check_reports=check_reports(candidate)
        )
        next_call = invocations.prepare(self.gh, candidate, "polish")
        self.assertEqual(next_call["source"], candidate["candidate"]["head"])
        change = report(next_call)
        change["changes"][0]["content"] = "print(sum(args), end='\\n')\n"
        invocations.record(self.gh, next_call, change)
        new_candidate = publish(self.gh, next_call, change)
        self.assertNotEqual(new_candidate["candidate"], candidate["candidate"])
        with self.assertRaisesRegex(Blocked, "Source changed"):
            invocations.record(self.gh, reviewer, report(reviewer))

    def test_missing_or_substituted_review_never_merges(self):
        context = self.candidate()
        key = context["candidate"]
        outcome = finish(self.gh, context, verified(key), reviewed(key))
        self.assertEqual(self.gh.merges, [])
        self.assertIn("recorded independent review", outcome["reason"])

    def test_review_must_consume_complete_current_command_results(self):
        with self.assertRaisesRegex(Blocked, "published candidate"):
            invocations.prepare(self.gh, self.context, "audit")
        candidate = self.candidate()
        with self.assertRaisesRegex(Blocked, "Missing, duplicate"):
            invocations.prepare(self.gh, candidate, "audit")
        self.assertEqual(self.gh.state["agent_calls"], 1)
        checks = check_reports(candidate)
        review_context = invocations.prepare(self.gh, candidate, "audit", check_reports=checks)
        result = report(review_context)
        invocations.record(self.gh, review_context, result)
        changed = combine_checks(candidate, checks)
        changed["checks"][0]["tests"] += 1
        outcome = finish(self.gh, candidate, changed, result)
        self.assertIn("recorded independent review", outcome["reason"])
        self.assertEqual(self.gh.merges, [])

    def test_report_substitution_or_context_mutation_is_rejected(self):
        context = invocations.prepare(self.gh, self.context, "edit")
        bad_context = deepcopy(context)
        bad_context["invocation"]["task"] = "Altered task"
        with self.assertRaisesRegex(Blocked, "context differs"):
            invocations.guard(self.gh, bad_context)
        value = report(context)
        with self.assertRaisesRegex(Blocked, "recorded source-editing"):
            publish(self.gh, context, value)
        changed = deepcopy(value)
        changed["invocation"]["id"] = "inspect"
        with self.assertRaisesRegex(Blocked, "provenance"):
            invocations.record(self.gh, context, changed)
        invocations.record(self.gh, context, value)
        changed = deepcopy(value)
        changed["changes"][0]["content"] = "changed after recording"
        with self.assertRaisesRegex(Blocked, "recorded source-editing"):
            publish(self.gh, context, changed)

    def test_read_only_task_cannot_approve_code_or_edit_files(self):
        context = invocations.prepare(self.gh, self.context, "inspect")
        value = report(context)
        value["unchanged"] = False
        with self.assertRaisesRegex(Blocked, "modified source"):
            invocations.record(self.gh, context, value)
        value["unchanged"] = True
        value["result"]["skills_used"] = ["nexkit-task"]
        with self.assertRaisesRegex(Blocked, "consumer skills"):
            invocations.record(self.gh, context, value)
        with self.assertRaisesRegex(Blocked, "recorded invocation"):
            invocations.prepare(self.gh, self.context, "edit", [value])

    def test_cas_reservation_and_record_retry_preserve_other_updates(self):
        save = self.gh.save_state
        seen = []

        def conflict(number, value, revision):
            if not seen:
                seen.append(True)
                self.gh.state["other_update"] = "preserved"
                self.gh.revision += 1
                raise Blocked("HTTP 409: competing update")
            return save(number, value, revision)

        with patch.object(self.gh, "save_state", side_effect=conflict):
            context = invocations.prepare(self.gh, self.context, "inspect")
            seen.clear()
            invocations.record(self.gh, context, report(context))
        self.assertEqual(self.gh.state["agent_calls"], 1)
        self.assertEqual(self.gh.state["other_update"], "preserved")

    def test_failure_retry_keeps_budget_and_feedback(self):
        first = invocations.prepare(self.gh, self.context, "edit")
        failed(self.gh, self.context, "CLI output missing")
        next_round = self.start("101.1")
        self.assertIn("CLI output missing", next_round["feedback"]["reason"])
        self.assertEqual(self.gh.state["agent_calls"], 1)
        invocations.prepare(self.gh, next_round, "edit")
        self.assertEqual(self.gh.state["agent_calls"], 2)
        with self.assertRaisesRegex(Blocked, "Superseded"):
            invocations.guard(self.gh, first)

    def test_budget_exhaustion_and_legacy_adapter_cannot_start_extra_calls(self):
        self.gh.state["delivery_calls"] = self.context["config"]["limits"]["agent_calls"]
        with self.assertRaisesRegex(Blocked, "budget exhausted"):
            invocations.prepare(self.gh, self.context, "inspect")
        gh = InvocationGitHub()
        result = prepare(gh, 1, "100.1", "a" * 40, pipeline="maintenance")
        self.assertFalse(result["ready"])
        self.assertNotIn("agent_calls", gh.state)

    def test_runtime_rejects_wrong_run_caller_and_forged_base_reservation(self):
        invocations.runtime_guard(self.gh, self.context, "a" * 40)
        with (
            patch.dict(os.environ, {"GITHUB_RUN_ATTEMPT": "2"}),
            self.assertRaisesRegex(Blocked, "run attempt"),
        ):
            invocations.runtime_guard(self.gh, self.context, "a" * 40)
        with (
            patch.dict(
                os.environ,
                {
                    "GITHUB_WORKFLOW_REF": "owner/project/.github/workflows/other.yml@refs/heads/main"
                },
            ),
            self.assertRaisesRegex(Blocked, "entrypoint"),
        ):
            invocations.runtime_guard(self.gh, self.context, "a" * 40)
        changed = deepcopy(self.context)
        changed["base"] = self.gh.branches["main"] = "d" * 40
        with self.assertRaisesRegex(Blocked, "accepted reservation"):
            revalidate(self.gh, changed)

    def test_preparation_outputs_do_not_require_dummy_global_models(self):
        gh = InvocationGitHub()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "event.json", {"inputs": {"issue": "1"}})
            env = {
                "GITHUB_EVENT_NAME": "workflow_dispatch",
                "GITHUB_REF": "refs/heads/main",
                "GITHUB_EVENT_PATH": str(root / "event.json"),
                "NEXKIT_PIPELINE": "maintenance",
                "GITHUB_OUTPUT": str(root / "outputs"),
            }
            with patch.dict(os.environ, env), patch("nexkit.ci.GitHub", return_value=gh):
                result = prepare_job(root / "context.json", "a" * 40, individual_agents=True)
            self.assertTrue(result["ready"])
            self.assertIn("ready=true", (root / "outputs").read_text())
            self.assertEqual(load_context(root), result)

    def test_bad_definitions_fail_before_any_cli_reservation(self):
        for change in (
            {"minutes": 0},
            {"task": "unaccepted.md"},
            {"task": []},
            {"contract": "approve"},
            {"model": ""},
            {"reasoning_effort": "imaginary"},
            {"setup": "shell string"},
            {"agent_runner": None},
            {"skills": ["unaccepted/SKILL.md"]},
        ):
            cfg = deepcopy(self.gh.cfg)
            cfg["pipelines"]["maintenance"]["invocations"]["edit"].update(change)
            with self.subTest(change=change), self.assertRaises(Blocked):
                config(cfg)

    def test_real_workspace_installs_accepted_task_and_skills_then_restores_them(self):
        context = invocations.prepare(self.gh, self.context, "inspect")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, home, data = root / "source", root / "home", root / "data"
            source.mkdir()
            home.mkdir()
            subprocess.run(["git", "init", "--quiet", str(source)], check=True)
            (source / "app.py").write_text("print(3)\n")
            subprocess.run(["git", "-C", str(source), "add", "app.py"], check=True)
            materialize(source, home / "work", context, "task", data)
            custom = home / "work/.agents/skills/domain-audit"
            self.assertTrue((custom / "references/contracts.md").is_file())
            self.assertIn(context["invocation"]["task"], (data / "prompt.txt").read_text())
            original = (custom / "SKILL.md").read_text()
            snapshot(home, data)
            (custom / "SKILL.md").write_text("Altered by untrusted setup")
            restore(home, data, uid=os.getuid(), gid=os.getgid(), role="task")
            self.assertEqual((custom / "SKILL.md").read_text(), original)
            write_json(root / "result.json", report(context)["result"])
            output = collect(
                source,
                home / "work",
                context,
                root / "result.json",
                root / "report.json",
                "task",
                data / "initial.json",
            )
            self.assertTrue(output["unchanged"])
            self.assertEqual(output["invocation"]["context"], digest(context))
            (home / "work/app.py").write_text("print(4)\n")
            changed = collect(
                source,
                home / "work",
                context,
                root / "result.json",
                root / "report.json",
                "task",
                data / "initial.json",
            )
            self.assertFalse(changed["unchanged"])
            self.assertEqual(
                read_json(data / "schema.json")["required"],
                ["status", "summary", "skills_used", "commands", "limitations"],
            )

    def test_cli_handles_zero_optional_reports_and_preserves_failure_feedback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "context.json", self.context)
            argv = [
                "ci",
                "prepare-invocation",
                "--context",
                str(root / "context.json"),
                "--invocation",
                "edit",
                "--inputs",
                "--reports",
                "--kit-ref",
                "a" * 40,
                "--out",
                str(root / "invocation.json"),
            ]
            with (
                patch("sys.argv", argv),
                patch("nexkit.ci.GitHub", return_value=self.gh),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                self.assertEqual(main(), 0)
            self.assertEqual(read_json(root / "invocation.json")["invocation"]["id"], "edit")
            argv = [
                "ci",
                "finish-work",
                "--context",
                str(root / "context.json"),
                "--reports",
                "--kit-ref",
                "a" * 40,
                "--out",
                str(root / "outcome.json"),
            ]
            with (
                patch("sys.argv", argv),
                patch("nexkit.ci.GitHub", return_value=self.gh),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                self.assertEqual(main(), 0)
            self.assertIn("No candidate", read_json(root / "outcome.json")["reason"])
            self.assertEqual(len(self.gh.dispatches), 1)

    def test_doctor_displays_each_effective_invocation_configuration(self):
        cfg = effective_config(self.gh.cfg, "maintenance")
        with tempfile.TemporaryDirectory() as directory:
            result = doctor(Path(directory), cfg)
        self.assertEqual(result["models"], {})
        self.assertEqual(result["invocations"]["edit"]["model"], "edit-model")
        self.assertEqual(result["invocations"]["edit"]["reasoning_effort"], "max")
        self.assertEqual(result["invocations"]["edit"]["agent_runner"], "ubuntu-24.04")
        self.assertFalse(result["live_agent_verified"])

    def test_runner_override_has_a_concrete_preview_without_provisioning(self):
        cfg = deepcopy(self.gh.cfg)
        cfg["defaults"]["engine"]["auth"] = "chatgpt"
        cfg["defaults"]["environment"]["agent_runner"] = [
            "self-hosted",
            "linux",
            "x64",
            "consumer-main",
        ]
        cfg["pipelines"]["maintenance"]["invocations"]["edit"]["agent_runner"] = [
            "self-hosted",
            "linux",
            "x64",
            "consumer-special",
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "project.json"
            write_json(path, cfg)
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/provision_runner.py",
                    "--config",
                    str(path),
                    "--pipeline",
                    "maintenance",
                    "--invocation",
                    "edit",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                capture_output=True,
                check=True,
            )
        plan = json.loads(result.stdout)
        self.assertEqual(plan["agent_runner"][-1], "consumer-special")
        self.assertEqual(plan["invocation"], "edit")
        self.assertEqual(plan["allowed_workflows"], ["changes.yml"])
        self.assertFalse(plan["applied"])
        self.assertFalse(plan["credentials_copied"])


class InvocationWorkflowTests(unittest.TestCase):
    def test_finalizer_shell_excludes_failed_downloads_even_when_partial_files_exist(self):
        root = Path(__file__).resolve().parents[1] / ".github/workflows"
        workflow = yaml.load((root / "finish-work.yml").read_text(), Loader=yaml.BaseLoader)
        finalizer = next(
            step for step in workflow["jobs"]["finish"]["steps"] if step.get("id") == "finish"
        )
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            data = temp / "nexkit"
            for relative in (
                "candidate/candidate.json",
                "checks/one/test.json",
                "checks/two/e2e.json",
                "review/audit.json",
            ):
                write_json(data / relative, {})
            binary = temp / "bin/python3"
            binary.parent.mkdir()
            binary.write_text(
                f"#!{sys.executable}\nimport json, os, sys\nfrom pathlib import Path\nPath(os.environ['NEXKIT_CAPTURE']).write_text(json.dumps(sys.argv[1:]))\n"
            )
            binary.chmod(0o755)
            capture = temp / "argv.json"
            for valid in (False, True):
                env = {
                    **os.environ,
                    "PATH": str(binary.parent) + ":" + os.environ["PATH"],
                    "NEXKIT_CAPTURE": str(capture),
                    "NEXKIT_REF": "a" * 40,
                }
                env.update(
                    {
                        name: str(valid).lower()
                        for name in (
                            "NEXKIT_JOBS_OK",
                            "NEXKIT_CANDIDATE_OK",
                            "NEXKIT_CHECKS_OK",
                            "NEXKIT_REVIEW_OK",
                        )
                    }
                )
                subprocess.run(
                    ["bash", "-e", "-c", finalizer["run"].replace("/tmp/nexkit", str(data))],
                    env=env,
                    check=True,
                )
                argv = read_json(capture)
                for flag in ("--jobs-succeeded", "--candidate", "--review"):
                    self.assertEqual(flag in argv, valid)
                self.assertEqual(str(data / "checks/one/test.json") in argv, valid)

    def test_repeated_checks_have_distinct_artifact_names_for_each_publication(self):
        root = Path(__file__).resolve().parents[1] / ".github/workflows"
        workflow = yaml.load((root / "candidate-check.yml").read_text(), Loader=yaml.BaseLoader)
        upload = next(
            step for step in workflow["jobs"]["check"]["steps"] if step.get("id") == "report"
        )
        template = upload["with"]["name"]
        names = [
            template.replace("${{ inputs.candidate_artifact_id }}", value)
            .replace("${{ inputs.check }}", "unit")
            .replace("${{ github.run_attempt }}", "1")
            for value in ("123", "456")
        ]
        self.assertNotEqual(*names)

    def test_failed_download_cannot_skip_failure_recording_or_supply_partial_evidence(self):
        root = Path(__file__).resolve().parents[1] / ".github/workflows"
        workflow = yaml.load((root / "finish-work.yml").read_text(), Loader=yaml.BaseLoader)
        finalizer = next(
            step for step in workflow["jobs"]["finish"]["steps"] if step.get("id") == "finish"
        )
        self.assertEqual(finalizer["if"], "always() && steps.round-context.outcome == 'success'")
        self.assertIn("steps.check-data.outcome == 'success'", finalizer["env"]["NEXKIT_JOBS_OK"])
        self.assertIn('if [ "$NEXKIT_CHECKS_OK" = true ]', finalizer["run"])
        self.assertIn('if [ "$NEXKIT_CANDIDATE_OK" = true ]', finalizer["run"])
        self.assertIn('if [ "$NEXKIT_REVIEW_OK" = true ]', finalizer["run"])

    def test_exact_download_rejects_implicit_all_missing_and_colliding_inputs(self):
        for value in ("", "123,123", "123garbage", "0", "-1", "123,456"):
            with self.subTest(value=value), self.assertRaises(Blocked):
                identifiers(value)
        self.assertEqual(identifiers(", ,", multiple=True, optional=True), [])
        self.assertEqual(identifiers("123, ,456", multiple=True), ["123", "456"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "one.json", {})
            self.assertEqual(len(downloaded(root, 1)), 1)
            with self.assertRaisesRegex(Blocked, "Missing"):
                downloaded(root, 2)
            write_json(root / "two.json", {})
            with self.assertRaisesRegex(Blocked, "own data file"):
                downloaded(root, 2)
            (root / "one").mkdir()
            (root / "two").mkdir()
            (root / "one.json").rename(root / "one/result.json")
            (root / "two.json").rename(root / "two/result.json")
            self.assertEqual(len(downloaded(root, 2)), 2)
            (root / "unexpected").symlink_to(root / "one/result.json")
            with self.assertRaisesRegex(Blocked, "symlink"):
                downloaded(root, 2)

    def test_native_agent_job_is_separate_from_state_and_publication_permissions(self):
        root = Path(__file__).resolve().parents[1] / ".github/workflows"
        doc = yaml.load((root / "agent-invocation.yml").read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(doc["jobs"]["execute"]["permissions"].values()), {"read"})
        self.assertEqual(doc["jobs"]["record"]["runs-on"], "ubuntu-24.04")
        self.assertEqual(doc["jobs"]["authorize"]["permissions"]["contents"], "write")
        for filename in (
            "prepare-work.yml",
            "agent-invocation.yml",
            "publish-candidate.yml",
            "finish-work.yml",
        ):
            workflow = yaml.load((root / filename).read_text(), Loader=yaml.BaseLoader)
            for job in workflow["jobs"].values():
                for step in job["steps"]:
                    if step.get("uses") == "./kit/actions/download-data":
                        self.assertIn("artifact-ids", step["with"])
                        self.assertNotIn("pattern", step["with"])


if __name__ == "__main__":
    unittest.main()
