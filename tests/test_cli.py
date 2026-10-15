import contextlib
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import nexkit
import nexkit.config
from nexkit import cli, scaffold
from nexkit.runtimes import DECLARED_SECRETS, RUNTIMES, SECRET_SLOTS
from tests.support import CLAUDE, PROFILES, FakeGitHub, GitRepos, git

ROOT = Path(__file__).resolve().parent.parent


class InitDoctorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_init_writes_config_and_workflow(self):
        code = cli.main(
            ["init", "--repo", str(self.root), "--check", "test=npm test", "--setup", "npm ci"]
        )
        self.assertEqual(code, 0)
        cfg = json.loads((self.root / ".nexkit/config.json").read_text())
        self.assertEqual(cfg["checks"], [{"name": "test", "run": "npm test"}])
        self.assertEqual(cfg["setup"], ["npm ci"])
        workflow = (self.root / ".github/workflows/nexkit.yml").read_text()
        tag = f"v{nexkit.__version__}"
        self.assertIn(f"uses: phuongnse/nexkit/.github/workflows/pipeline.yml@{tag}", workflow)
        self.assertIn(f"nexkit_ref: {tag}", workflow)
        self.assertNotIn("__KIT", workflow)
        self.assertEqual(cli.main(["init", "--repo", str(self.root)]), 2)  # refuses overwrite
        self.assertEqual(cli.main(["init", "--repo", str(self.root), "--force"]), 0)

    def test_init_rejects_bad_check(self):
        self.assertEqual(cli.main(["init", "--repo", str(self.root), "--check", "npm test"]), 2)
        self.assertFalse((self.root / ".nexkit").exists())

    def test_doctor_without_github(self):
        cli.main(["init", "--repo", str(self.root), "--check", "test=true"])
        git(self.root, "init", "-q")
        findings = scaffold.doctor(self.root)
        self.assertIn((True, ".nexkit/config.json is valid"), findings)
        self.assertTrue(any(f"pipeline at v{nexkit.__version__}" in m for ok, m in findings if ok))
        self.assertIn((False, "The 'origin' remote is not a GitHub repository"), findings)
        self.assertEqual(cli.main(["doctor", "--repo", str(self.root)]), 1)

    def test_doctor_checks_the_workflows_to_start_after_a_merge(self):
        cli.main(["init", "--repo", str(self.root), "--check", "test=true"])
        path = self.root / ".nexkit/config.json"
        cfg = json.loads(path.read_text())
        cfg["merge"]["after_workflows"] = ["ci.yml", "lint.yml", "e2e.yml"]
        path.write_text(json.dumps(cfg))
        workflows = self.root / ".github/workflows"
        (workflows / "ci.yml").write_text("on:\n  push:\n  workflow_dispatch:\n")
        (workflows / "lint.yml").write_text("# no workflow_dispatch yet\non: [push]\n")
        findings = scaffold.doctor(self.root)
        self.assertIn(
            (True, "merge.after_workflows: ci.yml can be started after a merge"), findings
        )
        self.assertIn(
            (
                False,
                "merge.after_workflows: lint.yml has no workflow_dispatch trigger, "
                "so NexKit cannot start it after a merge",
            ),
            findings,
        )
        self.assertIn(
            (False, "merge.after_workflows: .github/workflows/e2e.yml does not exist"), findings
        )

    def test_concurrency_group_is_on_the_job(self):
        cli.main(["init", "--repo", str(self.root), "--check", "test=true"])
        path = self.root / ".github/workflows/nexkit.yml"
        text = path.read_text()
        group = (
            "concurrency:\n      group: nexkit-${{ github.event.issue.number || "
            "github.event.pull_request.number || inputs.number || github.event_name }}\n"
        )
        self.assertIn("\n    " + group, text.split("\n  nexkit:\n", 1)[1])
        self.assertNotRegex(text, r"(?m)^concurrency:|cancel-in-progress")
        ok = (True, "Commands on one issue or pull request run one at a time")
        self.assertIn(ok, scaffold.doctor(self.root))
        # A workflow from NexKit 1.7 or earlier sets it for the whole workflow.
        path.write_text(text.replace("    " + group, "").replace("jobs:", "concurrency: x\njobs:"))
        self.assertNotIn(ok, scaffold.doctor(self.root))

    def test_doctor_checks_the_trigger_for_parent_issues(self):
        cli.main(["init", "--repo", str(self.root), "--check", "test=true"])
        path = self.root / ".nexkit/config.json"
        cfg = json.loads(path.read_text())
        cfg["close_parent_issues"] = True
        path.write_text(json.dumps(cfg))
        ok = (True, "Closed issues start NexKit, for close_parent_issues")
        self.assertIn(ok, scaffold.doctor(self.root))
        workflow = self.root / ".github/workflows/nexkit.yml"
        text = workflow.read_text()
        workflow.write_text(
            text.replace("  issues:\n    types: [closed]", "  issues:\n    types: [opened]")
        )
        findings = scaffold.doctor(self.root)
        self.assertNotIn(ok, findings)
        self.assertTrue(any("does not listen to 'issues: closed'" in m for _, m in findings))
        cfg["close_parent_issues"] = False
        path.write_text(json.dumps(cfg))
        self.assertFalse(any("issues: closed" in m for _, m in scaffold.doctor(self.root)))

    def test_doctor_checks_the_schedule_that_resumes_paused_runs(self):
        cli.main(["init", "--repo", str(self.root), "--check", "test=true"])
        ok = (True, "A schedule resumes runs paused at the agent's usage limit")
        self.assertIn(ok, scaffold.doctor(self.root))
        workflow = self.root / ".github/workflows/nexkit.yml"
        text = workflow.read_text()
        workflow.write_text(re.sub(r"  schedule:\n.*\n", "", text))
        findings = scaffold.doctor(self.root)
        self.assertTrue(any("has no 'schedule' trigger" in m for ok, m in findings if not ok))
        path = self.root / ".nexkit/config.json"
        cfg = json.loads(path.read_text())
        cfg["usage_limit"] = {"resume": False}
        path.write_text(json.dumps(cfg))
        self.assertFalse(any("schedule" in m for _, m in scaffold.doctor(self.root)))

    def test_init_listens_to_pushes_to_the_default_branch(self):
        git(self.root, "init", "-q", "-b", "trunk")
        cli.main(["init", "--repo", str(self.root), "--check", "test=true"])
        workflow = (self.root / ".github/workflows/nexkit.yml").read_text()
        self.assertIn("branches: [trunk]", scaffold.trigger(workflow, "push"))
        self.assertEqual(scaffold.default_branch_of(self.root / ".nexkit"), "trunk")

    def test_doctor_checks_the_push_trigger_for_conflicts(self):
        cli.main(["init", "--repo", str(self.root), "--check", "test=true"])
        path = self.root / ".nexkit/config.json"
        cfg = json.loads(path.read_text())
        cfg["conflicts"] = {"auto_resolve": True}
        path.write_text(json.dumps(cfg))
        ok = (True, "Pushes to the default branch start the conflict check")
        self.assertIn(ok, scaffold.doctor(self.root))
        workflow = self.root / ".github/workflows/nexkit.yml"
        workflow.write_text(re.sub(r"  push:\n.*\n", "", workflow.read_text()))
        findings = scaffold.doctor(self.root)
        self.assertTrue(any("has no 'push' trigger" in m for ok, m in findings if not ok))

    def test_doctor_with_github(self):
        cli.main(["init", "--repo", str(self.root), "--check", "test=true"])
        path = self.root / ".nexkit/config.json"
        cfg = json.loads(path.read_text())
        cfg["notify"] = ["owner", "reader"]
        path.write_text(json.dumps(cfg))
        answers = {
            ".default_branch": "main\n",
            "owner/permission": "admin\n",
            "reader/permission": "read\n",
        }

        def fake_gh(*args):
            if args[:2] == ("auth", "status"):
                return "Logged in"
            for key, value in answers.items():
                if any(key in arg for arg in args):
                    return value
            return None

        with (
            mock.patch.object(scaffold, "repository_of", return_value="acme/app"),
            mock.patch.object(scaffold, "_gh", side_effect=fake_gh),
        ):
            findings = scaffold.doctor(self.root)
            self.assertIn(
                (
                    False,
                    "Set a secret for the `claude` agent: run "
                    "'gh secret set CLAUDE_CODE_OAUTH_TOKEN --repo acme/app' with the token "
                    "from 'claude setup-token'",
                ),
                findings,
            )
            self.assertIn((True, "notify: @owner has write access"), findings)
            self.assertTrue(any("@reader has no write access" in m for ok, m in findings))
            self.assertFalse(any("does not list the default branch" in m for _, m in findings))
            answers[".default_branch"] = "trunk\n"
            findings = scaffold.doctor(self.root)
            self.assertTrue(any("does not list the default branch trunk" in m for _, m in findings))
            cfg["notify"] = []
            path.write_text(json.dumps(cfg))
            scaffold.doctor(self.root)  # no notify: the rest of the checks still run

    def test_doctor_checks_that_the_workflow_passes_each_agent_secret(self):
        cli.main(["init", "--repo", str(self.root), "--check", "test=true"])
        self.assertFalse(
            any("does not pass it" in m for _, m in scaffold.doctor(self.root)), "init passes all"
        )
        workflow = self.root / ".github/workflows/nexkit.yml"
        inherit = workflow.read_text()
        text = inherit.replace(
            "    secrets: inherit\n",
            "    secrets:\n      NEXKIT_PUSH_TOKEN: ${{ secrets.NEXKIT_PUSH_TOKEN }}\n",
        )
        workflow.write_text(text + "# ${{ secrets.ANTHROPIC_API_KEY }} in a comment\n")
        self.assertIn(
            (
                False,
                "The `claude` agent needs CLAUDE_CODE_OAUTH_TOKEN, but "
                ".github/workflows/nexkit.yml does not pass it to the pipeline; add it under "
                "'secrets:' as 'nexkit init' writes it",
            ),
            scaffold.doctor(self.root),
        )
        config = self.root / ".nexkit/config.json"
        azure = {"runtime": "codex", "provider": "azure-openai"}
        azure.update(endpoint_secret="AZURE_OPENAI_ENDPOINT", api_key_secret="GPT_SOL_KEY")
        raw = {**json.loads(config.read_text()), "agents": {"sol": azure}}
        config.write_text(json.dumps({**raw, "agent": "sol", "model": "gpt-sol"}))
        passed = "    secrets:\n      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}\n"
        workflow.write_text(inherit.replace("    secrets: inherit\n", passed))
        self.assertIn(
            (
                False,
                "The `sol` agent needs GPT_SOL_KEY, but .github/workflows/nexkit.yml does not "
                "pass it to the pipeline; write 'secrets: inherit' as 'nexkit init' writes it",
            ),
            scaffold.doctor(self.root),
        )
        workflow.write_text(inherit)
        self.assertFalse(any("does not pass it" in m for _, m in scaffold.doctor(self.root)))

    def test_doctor_checks_the_push_token(self):
        cfg = nexkit.config.validate({"agents": CLAUDE})
        names = {"CLAUDE_CODE_OAUTH_TOKEN", "NEXKIT_PUSH_TOKEN"}
        unused = scaffold._secrets_set(cfg, names, "acme/app")[-1]
        self.assertFalse(unused[0])
        self.assertIn("publish.push_token_secret does not name it", unused[1])
        cfg["publish"]["push_token_secret"] = "BOT_TOKEN"
        self.assertIn(
            "names BOT_TOKEN, which is not set",
            scaffold._secrets_set(cfg, names, "acme/app")[-1][1],
        )
        found = scaffold._secrets_set(cfg, {*names, "BOT_TOKEN"}, "acme/app")[-1]
        self.assertEqual(found, (True, "Pushes use the BOT_TOKEN secret"))
        listed = (
            "    secrets:\n      CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}\n"
        )
        self.assertIn(
            "publish.push_token_secret needs BOT_TOKEN",
            scaffold._passed_secrets(cfg, listed)[0][1],
        )

    def test_doctor_finds_the_secret_of_each_agent(self):
        cfg = nexkit.config.validate(
            {
                "agents": {
                    **CLAUDE,
                    "backup": {
                        "runtime": "claude-code",
                        "provider": "anthropic",
                        "api_key_secret": "ANTHROPIC_API_KEY",
                    },
                },
                "stages": {"review": {"agent": "backup", "model": "opus"}},
            }
        )
        self.assertEqual(
            scaffold._secrets_set(cfg, {"ANTHROPIC_API_KEY", "OTHER"}, "acme/app"),
            [
                (
                    False,
                    "Set a secret for the `claude` agent: run 'gh secret set "
                    "CLAUDE_CODE_OAUTH_TOKEN --repo acme/app' with the token from "
                    "'claude setup-token'",
                ),
                (True, "Secret for the `backup` agent is set: ANTHROPIC_API_KEY"),
            ],
        )

    def test_trigger(self):
        workflow = (
            "on:\n  push:\n    branches: [main] # x\n  schedule:\n    - cron: '1 * * * *'\njobs:\n"
        )
        self.assertIn("branches: [main]", scaffold.trigger(workflow, "push"))
        self.assertIsNotNone(scaffold.trigger(workflow, "schedule"))
        self.assertIsNone(scaffold.trigger(workflow, "issues"))
        self.assertIsNone(scaffold.trigger("on:\n  # issues:\njobs:\n", "issues"))

    def test_repository_of(self):
        git(self.root, "init", "-q")
        git(self.root, "remote", "add", "origin", "git@github.com:acme/app.git")
        self.assertEqual(scaffold.repository_of(self.root), "acme/app")


class PipelineCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.output = self.root / "output.txt"
        self.gh = FakeGitHub()
        self.gh.add_issue(5)
        cli.main(["init", "--repo", str(self.root / "repo"), "--check", "test=true"])
        patcher = mock.patch.object(cli, "_gh", return_value=self.gh)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def outputs(self):
        text = self.output.read_text()
        pattern = r"^(\w+)<<(EOF_\w+)\n(.*?)\n\2$"
        return {key: value for key, _, value in re.findall(pattern, text, re.M | re.S)}

    def run_route(self, event_name, event):
        path = self.root / "event.json"
        path.write_text(json.dumps(event))
        env = {
            "GITHUB_EVENT_NAME": event_name,
            "GITHUB_EVENT_PATH": str(path),
            "GITHUB_OUTPUT": str(self.output),
        }
        with mock.patch.dict(os.environ, env):
            self.assertEqual(cli.main(["route", "--repo", str(self.root / "repo")]), 0)
        return self.outputs()

    def test_route_outputs(self):
        event = {
            "action": "created",
            "issue": {"number": 5},
            "comment": {"id": 3, "body": "/nexkit go", "user": {"login": "alice", "type": "User"}},
        }
        out = self.run_route("issue_comment", event)
        self.assertEqual(out["action"], "implement")
        self.assertNotIn("claude_version", out)
        claude = ["CLAUDE_CODE_OAUTH_TOKEN"]
        self.assertEqual(json.loads(out["agent_secrets"]), claude)
        self.assertEqual(json.loads(out["review_secrets"]), claude)
        self.assertEqual(json.loads(out["decision"])["issue"], 5)
        self.assertEqual(json.loads(out["config"])["checks"][0]["name"], "test")
        self.assertEqual(out["agent_ref"], "main")
        self.assertEqual(out["agent_timeout"], "75")

        self.assertEqual(self.gh.reactions, [(3, "eyes")])
        self.assertEqual(len(self.gh.run_comments(5)), 1)

    def test_route_allows_for_the_setup_timeout(self):
        event = {
            "action": "created",
            "issue": {"number": 5},
            "comment": {"id": 3, "body": "/nexkit go", "user": {"login": "alice"}},
        }
        out = self.run_route("issue_comment", event)
        self.assertEqual(out["checks_timeout"], str(15 + 30 + 10))
        # The setup timeout counts toward the jobs that run setup.
        path = self.root / "repo/.nexkit/config.json"
        raw = json.loads(path.read_text())
        raw["limits"] = {"setup_timeout_minutes": 10}
        path.write_text(json.dumps(raw))
        out = self.run_route("issue_comment", event)
        self.assertEqual((out["agent_timeout"], out["checks_timeout"]), ("55", str(15 + 10 + 10)))

    def test_route_allows_for_triage_and_the_longest_plan(self):
        path = self.root / "repo/.nexkit/config.json"
        raw = json.loads(path.read_text())
        raw.update(profiles=PROFILES, default_profile="standard")
        path.write_text(json.dumps(raw))
        event = {
            "action": "created",
            "issue": {"number": 5},
            "comment": {"id": 3, "body": "/nexkit plan", "user": {"login": "alice"}},
        }
        out = self.run_route("issue_comment", event)
        self.assertEqual(out["agent_timeout"], str(5 + 40 + 30))
        self.assertIsNone(json.loads(out["decision"])["previous_profile"])
        # Other runs get the profile's settings in the configuration they pass on.
        event["comment"]["body"] = "/nexkit go"
        out = self.run_route("issue_comment", event)
        self.assertEqual(json.loads(out["decision"])["profile"], "standard")
        cfg = json.loads(out["config"])
        self.assertEqual(nexkit.config.stage(cfg, "implement")["model"], "sonnet")

    def test_route_passes_each_job_the_secrets_of_its_agents(self):
        from tests.test_codex import MixedRuntimeTests

        path = self.root / "repo/.nexkit/config.json"
        path.write_text(json.dumps({**json.loads(path.read_text()), **MixedRuntimeTests.RAW}))
        claude = ["CLAUDE_CODE_OAUTH_TOKEN"]
        azure = ["AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT"]
        event = {
            "action": "created",
            "issue": {"number": 5},
            "comment": {"id": 3, "body": "/nexkit plan", "user": {"login": "alice"}},
        }
        out = self.run_route("issue_comment", event)
        self.assertEqual(json.loads(out["agent_secrets"]), sorted([*claude, *azure]))
        # Without a plan, go runs on default_profile: implement and review on Claude.
        event["comment"]["body"] = "/nexkit go"
        out = self.run_route("issue_comment", event)
        self.assertEqual(json.loads(out["agent_secrets"]), claude)
        self.assertEqual(json.loads(out["review_secrets"]), claude)

    def test_route_on_pull_request_marks_the_round_running(self):
        self.gh.add_pull(6, 5, head_sha="b" * 40)
        event = {
            "action": "created",
            "issue": {"number": 6, "pull_request": {}},
            "comment": {"id": 4, "body": "/nexkit review", "user": {"login": "alice"}},
        }
        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "acme/app", "GITHUB_RUN_ID": "9"}):
            out = self.run_route("issue_comment", event)
        self.assertEqual(out["action"], "review")
        self.assertEqual(self.gh.reactions, [(4, "eyes")])
        self.assertEqual({s["state"] for s in self.gh.statuses}, {"pending"})
        self.assertTrue(self.gh.comments_matching(6, "actions/runs/9"))

    def test_route_hands_closed_issues_to_maintain_only_when_enabled(self):
        event = {"action": "closed", "issue": {"number": 5}}
        out = self.run_route("issues", event)
        self.assertEqual(out["action"], "none")
        self.assertFalse(self.gh.issue_comments[5])
        path = self.root / "repo/.nexkit/config.json"
        raw = json.loads(path.read_text())
        raw["close_parent_issues"] = True
        path.write_text(json.dumps(raw))
        out = self.run_route("issues", event)
        self.assertEqual(out["action"], "maintain")
        self.assertEqual(json.loads(out["decision"])["task"], "parents")
        self.assertEqual(out["maintain_timeout"], "15")
        self.assertFalse(self.gh.issue_comments[5])  # no run comment, no reaction

    def test_route_gives_the_waiting_run_its_wait(self):
        event = {"inputs": {"command": "resume", "number": "resume"}}
        out = self.run_route("workflow_dispatch", event)
        self.assertEqual(json.loads(out["decision"]), {"action": "maintain", "task": "resume"})
        self.assertEqual(out["maintain_timeout"], str(330 + 15))

    def test_route_replies_with_configuration_errors(self):
        (self.root / "repo/.nexkit/config.json").write_text('{"model": 1}')
        event = {
            "action": "created",
            "issue": {"number": 5},
            "comment": {
                "id": 3,
                "body": "/nexkit plan",
                "user": {"login": "alice", "type": "User"},
            },
        }
        out = self.run_route("issue_comment", event)
        self.assertEqual(out["action"], "none")
        self.assertTrue(self.gh.comments_matching(5, "configuration error"))

    def test_agent_setup_failure_is_redacted_and_summarised(self):
        setup = 'echo "key $DEPLOY_TOKEN, agent [$ANTHROPIC_API_KEY]"; exit 4'
        cfg = nexkit.config.validate({"agents": CLAUDE, "setup": [setup]})
        out = self.root / "out"
        out.mkdir()
        (out / "context.json").write_text("{}")
        summary = self.root / "summary.md"
        env = {
            "NEXKIT_DECISION": json.dumps({"action": "implement", "base": "main"}),
            "NEXKIT_CONFIG": json.dumps(cfg),
            "DEPLOY_TOKEN": "fake-deploy-token-0123456789",
            "ANTHROPIC_API_KEY": "fake-api-key-0123456789",
            "GITHUB_STEP_SUMMARY": str(summary),
        }
        stdout = io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(stdout):
            code = cli.main(["agent", "--repo", str(self.root), "--out", str(out)])
        self.assertEqual(code, 0)
        result = (out / "result.json").read_text()
        self.assertIn("exit code 4", result)
        for text in (stdout.getvalue(), result, summary.read_text()):
            self.assertNotIn("fake-deploy-token-0123456789", text)
            # Setup never sees the agent's credential.
            self.assertIn("key ***, agent []", text)
        self.assertIn("### NexKit implement: error", summary.read_text())

    def test_agent_setup_gets_the_base_commit(self):
        repos = GitRepos()
        self.addCleanup(repos.cleanup)
        repo = repos.clone("agent")  # implement starts on the base branch itself
        cfg = nexkit.config.validate(
            {"agents": CLAUDE, "setup": ['echo "base=$NEXKIT_BASE_SHA"; exit 4']}
        )
        out = self.root / "out"
        out.mkdir()
        (out / "context.json").write_text("{}")
        env = {
            "NEXKIT_DECISION": json.dumps({"action": "implement", "base": "main"}),
            "NEXKIT_CONFIG": json.dumps(cfg),
            "GITHUB_STEP_SUMMARY": str(self.root / "summary.md"),
        }
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
            cli.main(["agent", "--repo", str(repo), "--out", str(out)])
        result = json.loads((out / "result.json").read_text())
        self.assertIn(f"base={git(repo, 'rev-parse', 'HEAD')}", result["error"])

    def test_checks_get_the_merge_base_only_when_it_is_known(self):
        repos = GitRepos()
        self.addCleanup(repos.cleanup)
        fork = git(repos.root / "seed", "rev-parse", "HEAD")
        repos.push_commit("dev", "nexkit/issue-5", {"a.txt": "a\n"}, "A")
        repos.push_commit("dev", "main", {"b.txt": "b\n"}, "B")
        repo = repos.clone("verify")
        git(repo, "checkout", "-q", "nexkit/issue-5")
        cfg = nexkit.config.validate(
            {
                "agents": CLAUDE,
                "checks": [{"name": "base", "run": 'echo "${NEXKIT_BASE_SHA-unset}"'}],
            }
        )
        for base, seen in (("main", fork), ("gone", "unset")):
            env = {
                "NEXKIT_DECISION": json.dumps({"action": "review", "base": base}),
                "NEXKIT_CONFIG": json.dumps(cfg),
                "NEXKIT_BASE_SHA": "stale",
            }
            stdout = io.StringIO()
            with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(stdout):
                cli.main(["checks", "--repo", str(repo), "--out", str(self.root / "out")])
            checks = json.loads((self.root / "out" / "checks.json").read_text())
            self.assertEqual(checks[0]["output"].strip(), seen)
        self.assertIn("NEXKIT_BASE_SHA is not set", stdout.getvalue())


class InstallAgentsTests(unittest.TestCase):
    def test_installs_each_runtime_the_config_uses(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "path"
            env = {
                "NEXKIT_CONFIG": json.dumps(nexkit.config.validate({"agents": CLAUDE})),
                "GITHUB_PATH": str(path),
            }
            with (
                mock.patch.dict(os.environ, env),
                mock.patch("subprocess.run") as run,
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.assertEqual(cli.main(["install-agents"]), 0)
            commands, bin_dir = RUNTIMES["claude-code"].install()
            self.assertEqual([c.args[0] for c in run.call_args_list], commands)
            self.assertEqual(run.call_args.kwargs, {"cwd": Path.home(), "check": True})
            self.assertEqual(path.read_text(), f"{bin_dir}\n")
        self.assertIn(f"Installing claude-code {nexkit.CLAUDE_CODE}", stdout.getvalue())
        self.assertEqual(commands[0][-1], nexkit.CLAUDE_CODE)


class SupportedVersionTests(unittest.TestCase):
    """The workflows use the tested versions, and the Claude Code pin has one source."""

    def test_workflows_use_the_tested_versions(self):
        wanted_python = ".".join(map(str, nexkit.PYTHON))
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            text = path.read_text()
            self.assertEqual(set(re.findall(r"runs-on: (\S+)", text)), {nexkit.RUNNER}, path.name)
            self.assertLessEqual(
                set(re.findall(r'python-version: "([^"]+)"', text)), {wanted_python}, path.name
            )
            self.assertNotIn("python3 ", text, path.name)
            # Runtime versions are read from nexkit/__init__.py, never repeated.
            for runtime in RUNTIMES.values():
                self.assertNotIn(runtime.version, text, path.name)

    def test_pyproject_declares_the_minimum_python(self):
        text = (ROOT / "pyproject.toml").read_text()
        major, minor = nexkit.PYTHON
        self.assertIn(f'requires-python = ">={major}.{minor}"', text)
        self.assertIn(f'target-version = "py{major}{minor}"', text)

    def test_older_python_versions_are_refused(self):
        self.assertTrue(nexkit.python_supported((*nexkit.PYTHON, 7, "final", 0)))
        self.assertTrue(nexkit.python_supported((3, 14, 0, "final", 0)))
        self.assertFalse(nexkit.python_supported((3, 11, 9, "final", 0)))
        with mock.patch.object(cli.sys, "version_info", (3, 11, 9, "final", 0)):
            self.assertEqual(cli.main(["--version"]), 2)


class WorkflowSafetyTests(unittest.TestCase):
    """Credential separation is the pipeline's core safety property; keep it explicit."""

    @classmethod
    def setUpClass(cls):
        cls.text = (ROOT / ".github/workflows/pipeline.yml").read_text()
        body = cls.text.split("\njobs:\n", 1)[1]
        cls.jobs = dict(re.findall(r"^  (\w+):\n((?:    .*\n|\n)*)", body, re.M))

    def test_jobs(self):
        self.assertEqual(
            list(self.jobs),
            ["route", "agent", "publish", "verify", "review", "report", "maintain"],
        )

    def test_jobs_that_run_agents_or_repository_code_cannot_write(self):
        for name in ("agent", "verify", "review"):
            self.assertNotIn(": write", self.jobs[name], name)
            self.assertNotIn("persist-credentials: true", self.jobs[name], name)
            self.assertNotIn("NEXKIT_PUSH_TOKEN", self.jobs[name], name)

    def test_verify_holds_no_secrets(self):
        self.assertNotIn("secrets.", self.jobs["verify"])
        self.assertNotIn("github.token", self.jobs["verify"])

    def test_jobs_with_write_tokens_never_receive_model_credentials(self):
        push = "secrets[fromJSON(needs.route.outputs.config).publish.push_token_secret]"
        for name in ("publish", "report", "route", "maintain"):
            job = self.jobs[name].replace(push, "")
            for text in (
                "CLAUDE_CODE_OAUTH_TOKEN",
                "ANTHROPIC_API_KEY",
                "secrets[",
                "NEXKIT_AGENT_SECRET",
            ):
                self.assertNotIn(text, job, name)
        # Publish reads only the push token, by the name the configuration gives it.
        self.assertEqual(self.jobs["publish"].count(push), 2)
        self.assertNotIn("secrets.", self.jobs["publish"])

    def test_the_pipeline_declares_the_secrets_a_workflow_may_pass_one_by_one(self):
        declared = self.text.split("    secrets:\n", 1)[1].split("\npermissions:", 1)[0]
        names = re.findall(r"^      (\w+):$", declared, re.M)
        self.assertEqual(names, [*DECLARED_SECRETS, "NEXKIT_PUSH_TOKEN"])
        # A new workflow passes every secret, so agents can use secrets of any name.
        template = (ROOT / "nexkit/templates/workflow.yml").read_text()
        self.assertIn("\n    secrets: inherit\n", template)

    def test_only_the_steps_that_run_an_agent_receive_agent_secrets(self):
        steps = [(n, s) for n, job in self.jobs.items() for s in job.split("\n      - ")]
        receiving = [(n, s) for n, s in steps if "NEXKIT_AGENT_SECRET" in s]
        self.assertEqual([n for n, _ in receiving], ["agent", "review"])
        for (job, step), needed in zip(receiving, ("agent_secrets", "review_secrets"), strict=True):
            self.assertIn("kit/bin/nexkit agent ", step)
            # Only the names route lists for the job's stages, each read into a slot.
            names = f"needs.route.outputs.{needed}"
            self.assertIn(f"NEXKIT_AGENT_SECRETS: ${{{{ {names} }}}}\n", step)
            slots = re.findall(
                r"NEXKIT_AGENT_SECRET_(\d+): \$\{\{ secrets\[fromJSON\((.*?)\)\[(\d+)\]\] \}\}",
                step,
            )
            self.assertEqual(slots, [(str(i + 1), names, str(i)) for i in range(SECRET_SLOTS)], job)
            self.assertEqual(re.findall(r"secrets\.\w+", step), [], job)

    def test_agent_jobs_install_the_runtimes_with_nexkit(self):
        for name in ("agent", "review"):
            self.assertIn("kit/bin/nexkit install-agents\n", self.jobs[name])
            self.assertNotIn("curl ", self.jobs[name])

    def test_jobs_with_write_tokens_run_only_nexkit(self):
        for name in ("route", "publish", "report", "maintain"):
            runs = re.findall(r"^\s+run: (.*)$", self.jobs[name], re.M)
            self.assertTrue(runs, name)
            for run in runs:
                self.assertTrue(
                    run.startswith("${{ steps.python.outputs.python-path }} kit/bin/nexkit "),
                    f"{name}: {run}",
                )

    def test_only_publish_and_report_may_write_contents(self):
        writers = [name for name, job in self.jobs.items() if "contents: write" in job]
        self.assertEqual(writers, ["publish", "report"])

    def test_report_merges_without_checking_out_the_repository(self):
        # `contents: write` lets report merge; it has nothing of the repository to run or push.
        report = self.jobs["report"]
        self.assertIn("contents: write", report)
        self.assertIn("actions: write", report)  # workflow_dispatch after the merge
        self.assertEqual(report.count("actions/checkout@"), 1)
        self.assertIn("path: kit", report)
        self.assertNotIn("persist-credentials: true", report)
        self.assertNotIn("NEXKIT_PUSH_TOKEN", report)

    def test_maintain_never_checks_out_the_repository(self):
        maintain = self.jobs["maintain"]
        self.assertIn("if: needs.route.outputs.action == 'maintain'", maintain)
        self.assertIn("fromJSON(needs.route.outputs.maintain_timeout)", maintain)
        self.assertEqual(maintain.count("actions/checkout@"), 1)
        self.assertIn("path: kit", maintain)
        self.assertNotIn("contents: write", maintain)
        self.assertNotIn("secrets.", maintain)
        self.assertIn('["none","maintain"]', self.jobs["report"])

    def test_route_acknowledges_commands_without_repository_code(self):
        route = self.jobs["route"]
        for permission in ("issues: write", "pull-requests: write", "statuses: write"):
            self.assertIn(permission, route)
        self.assertNotIn("contents: write", route)
        self.assertIn("sparse-checkout: .nexkit", route)

    def test_review_reads_the_fix_result(self):
        review = self.jobs["review"]
        self.assertIn("name: nexkit-agent\n          path: agent\n", review)
        self.assertIn("--stage review --fix-result agent/result.json", review)

    def test_agent_step_has_no_github_token(self):
        step = self.jobs["agent"].split("- name: Run agent\n", 1)[1].split("- uses:", 1)[0]
        self.assertNotIn("GITHUB_TOKEN", step)
        review = self.jobs["review"].split("- name: Run agent review", 1)[1]
        self.assertNotIn("GITHUB_TOKEN", review.split("- uses:", 1)[0])

    def test_report_downloads_each_artifact_into_its_own_directory(self):
        # With `pattern`, a single match is extracted without its directory.
        self.assertNotIn("pattern:", self.jobs["report"])
        for name in ("nexkit-agent", "nexkit-checks", "nexkit-review"):
            self.assertIn(f"name: {name}\n          path: artifacts/{name}\n", self.jobs["report"])

    def test_report_downloads_only_artifacts_of_stages_that_ran(self):
        # A download of an artifact that cannot exist adds an error annotation to every run.
        producers = {"nexkit-agent": "agent", "nexkit-checks": "verify", "nexkit-review": "review"}
        steps = self.jobs["report"].split("\n      - ")
        downloads = [step for step in steps if "actions/download-artifact@" in step]
        self.assertEqual(len(downloads), len(producers))
        for step in downloads:
            name = re.search(r"^\s+name: (\S+)$", step, re.M).group(1)
            self.assertIn(f"\n        if: needs.{producers[name]}.result != 'skipped'\n", step)
        for name, job in producers.items():
            upload = self.jobs[job].split("actions/upload-artifact@", 1)[1]
            self.assertIn("\n        if: always()\n", upload.split("\n      - ", 1)[0], job)
            self.assertIn(f"name: {name}\n", upload, job)

    def test_fix_rounds_check_out_the_history_they_merge(self):
        checkout = self.jobs["agent"].split("ref: ${{ needs.route.outputs.agent_ref }}", 1)[1]
        self.assertIn(
            "fetch-depth: ${{ needs.route.outputs.action == 'fix' && '0' || '1' }}",
            checkout.split("- name:", 1)[0],
        )
        self.assertIn("fetch-depth: 0", self.jobs["publish"])
        self.assertIn("fetch-depth: 0", self.jobs["review"])
        # The merge base with the base branch, for NEXKIT_BASE_SHA in the checks.
        self.assertIn("fetch-depth: 0", self.jobs["verify"])
        checks = self.jobs["verify"].split("- name: Run setup and checks", 1)[1]
        self.assertIn("NEXKIT_DECISION: ${{ needs.route.outputs.decision }}", checks)

    def test_agent_and_verify_restore_caches_but_never_save(self):
        # Both jobs run code the agent wrote; a cache they saved would reach the base branch.
        uses = "uses: ./kit/.github/actions/restore-caches\n"
        setup = {"agent": "kit/bin/nexkit agent --repo", "verify": "kit/bin/nexkit checks --repo"}
        for name, job in self.jobs.items():
            if name not in setup:
                self.assertNotIn("restore-caches", job, name)
                continue
            self.assertEqual(job.count(uses), 1, name)
            self.assertLess(job.index(uses), job.index(setup[name]), name)
            step = job.split("- name: Restore caches\n", 1)[1].split("\n      - ", 1)[0]
            self.assertIn("continue-on-error: true", step, name)
            self.assertIn("fromJSON(needs.route.outputs.config).cache[0]", step, name)
        # A plan runs no setup.
        self.assertIn("if: needs.route.outputs.action != 'plan' &&", self.jobs["agent"])

        action = (ROOT / ".github/actions/restore-caches/action.yml").read_text()
        for text in (self.text, action):
            self.assertNotRegex(text, r"actions/cache(/save)?@")
            self.assertNotIn("save-always", text)
        self.assertNotIn("secrets.", action)
        self.assertNotIn("github.token", action)
        steps = action.split("\n    - ")[1:]
        restores = [step for step in steps if "uses: actions/cache/restore@" in step]
        # One step for each entry that `cache` may have.
        self.assertEqual(len(restores), nexkit.config.MAX_CACHES)
        for number, step in enumerate(restores, 1):
            self.assertTrue(step.startswith(f"id: cache{number}\n"), step)
            self.assertIn(f"if: steps.plan.outputs.name{number}\n", step)
            self.assertIn("continue-on-error: true", step)
            self.assertIn(f"hashFiles(steps.plan.outputs.files{number})", step)
        self.assertIn("cache --report", steps[-1])

    def test_config_comes_from_default_branch(self):
        self.assertIn("ref: ${{ github.event.repository.default_branch }}", self.jobs["route"])


if __name__ == "__main__":
    unittest.main()
