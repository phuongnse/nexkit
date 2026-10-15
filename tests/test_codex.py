import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nexkit import agent, config, runlog
from nexkit.redact import Redactor
from nexkit.runtimes import RUNTIMES
from nexkit.runtimes.codex import CodexReader
from nexkit.schema import strict
from tests.support import AZURE, FakeCodex, GitRepos, codex_events, make_config
from tests.test_agent import CONTEXT, DONE, PLAN

ERROR = "unexpected status 401 Unauthorized: Access denied due to invalid subscription key"
# Recorded from Codex 0.162.1 against a local Responses API.
RECORDED = [
    {"type": "thread.started", "thread_id": "01a13d5f-9e0f-7cf3-9e07-77459ee1eca7"},
    {
        "type": "item.completed",
        "item": {
            "id": "item_0",
            "type": "error",
            "message": "Model metadata for `my-deploy` not found. Defaulting to fallback metadata.",
        },
    },
    {"type": "turn.started"},
    {
        "type": "item.started",
        "item": {
            "id": "item_1",
            "type": "command_execution",
            "command": "/bin/bash -lc 'ls -1'",
            "aggregated_output": "",
            "exit_code": None,
            "status": "in_progress",
        },
    },
    {
        "type": "item.completed",
        "item": {
            "id": "item_1",
            "type": "command_execution",
            "command": "/bin/bash -lc 'ls -1'",
            "aggregated_output": "a.txt\n",
            "exit_code": 0,
            "status": "completed",
        },
    },
    {
        "type": "item.completed",
        "item": {"id": "item_2", "type": "agent_message", "text": json.dumps(PLAN)},
    },
    {
        "type": "turn.completed",
        "usage": {
            "input_tokens": 200,
            "cached_input_tokens": 20,
            "cache_write_input_tokens": 0,
            "output_tokens": 40,
            "reasoning_output_tokens": 10,
        },
    },
]
FAILED = [
    {"type": "thread.started", "thread_id": "t"},
    {"type": "turn.started"},
    {"type": "error", "message": f"Reconnecting... 5/5 ({ERROR})"},
    {"type": "error", "message": ERROR},
    {"type": "turn.failed", "error": {"message": ERROR}},
]


def sol_config(**raw):
    return make_config(agents={"sol": AZURE}, agent="sol", model="gpt-6-1-sol", **raw)


def read(events, returncode=0, stderr=""):
    reader = CodexReader()
    shown = [event for raw in events for event in reader.events(raw)]
    return shown, reader.outcome(returncode, stderr)


class ConfigTests(unittest.TestCase):
    def test_an_azure_agent(self):
        cfg = sol_config(effort="xhigh", stages={"review": {"agent": "claude", "model": "opus"}})
        self.assertEqual(config.stage(cfg, "implement")["agent"], "sol")
        self.assertEqual(config.runtimes(cfg), ["claude-code", "codex"])
        self.assertEqual(
            config.credentials(cfg),
            [
                ("claude", ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")),
                ("sol", ("AZURE_OPENAI_ENDPOINT",)),
                ("sol", ("AZURE_OPENAI_API_KEY",)),
            ],
        )

    def test_one_endpoint_with_a_key_per_deployment(self):
        sol = {**AZURE, "api_key_secret": "GPT_SOL_KEY"}
        cfg = make_config(
            agents={"sol": sol, "max": {**AZURE, "api_key_secret": "GPT_MAX_KEY"}},
            stages={
                "implement": {"agent": "max", "model": "gpt-max"},
                "review": {"agent": "sol", "model": "gpt-sol"},
            },
        )
        self.assertEqual(
            config.secret_names([cfg], ["implement", "review"]),
            ["AZURE_OPENAI_ENDPOINT", "GPT_MAX_KEY", "GPT_SOL_KEY"],
        )

    def test_one_job_holds_at_most_the_slots(self):
        agents = {f"a{n}": {**AZURE, "api_key_secret": f"KEY_{n}"} for n in range(8)}
        profiles = {
            name: {"when": "x", "stages": {"plan": {"agent": name, "model": "m"}}}
            for name in agents
        }
        with self.assertRaisesRegex(config.ConfigError, "The plan stage can need 11 agent secrets"):
            config.validate({"agents": agents, "profiles": profiles, "default_profile": "a0"})

    def test_rejects_what_codex_or_azure_cannot_honour(self):
        def sol(**changes):
            return {"agents": {"sol": {**AZURE, **changes}}, "agent": "sol", "model": "m"}

        cases = [
            # The endpoint is personal: it lives in a secret, never in the configuration.
            (sol(endpoint="https://acme.openai.azure.com"), "Unknown keys in agents.sol: endpoint"),
            (sol(endpoint_secret="https://acme.openai.azure.com"), "holds only its name"),
            (sol(api_key_secret="0123456789abcdef0123456789abcdef"), "holds only its name"),
            (sol(api_key_secret="NEXKIT_PUSH_TOKEN"), "reserved for GitHub and NexKit"),
            (sol(api_version="2025-04-01"), "Unknown keys in agents.sol: api_version"),
            (
                {"agents": {"sol": {k: v for k, v in AZURE.items() if k != "endpoint_secret"}}},
                "endpoint_secret is required",
            ),
            (sol(runtime="claude-code"), "provider must be one of anthropic for claude-code"),
            (sol(provider="anthropic"), "provider must be one of azure-openai for codex"),
            ({**sol(), "effort": "max"}, r"stages\.plan\.effort must be one of none, .* for Codex"),
            (
                {**sol(), "stages": {"implement": {"max_budget_usd": 5}}},
                "stages.implement.max_budget_usd: Codex has no spending limit",
            ),
        ]
        for raw, message in cases:
            with self.subTest(raw=raw), self.assertRaisesRegex(config.ConfigError, message):
                config.validate(raw)


class MixedRuntimeTests(unittest.TestCase):
    RAW = {
        "agents": {"sol": AZURE},
        "stages": {"triage": {"agent": "sol", "model": "gpt-mini"}},
        "default_profile": "claude",
        "profiles": {
            "claude": {
                "when": "x",
                "stages": {"implement": {"model": "opus", "effort": "max", "max_budget_usd": 5}},
            },
            "gpt": {"when": "y", "stages": {"implement": {"agent": "sol", "model": "gpt"}}},
        },
    }

    def test_jobs_validate_the_layered_configuration_again(self):
        cfg = config.validate(self.RAW)
        for name in cfg["profiles"]:
            layered = config.with_profile(cfg, name)
            again = config.validate(json.loads(json.dumps(layered)), layered=True)
            self.assertEqual(again, layered, name)
        # Layering another profile over a layered one is what a fresh check would do.
        with self.assertRaisesRegex(config.ConfigError, "for Codex"):
            config.validate(json.loads(json.dumps(config.with_profile(cfg, "claude"))))

    def test_each_job_gets_only_the_secrets_its_stages_need(self):
        cfg = config.validate(self.RAW)
        plan = [cfg, *(config.with_profile(cfg, name) for name in cfg["profiles"])]
        claude = ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"]
        azure = ["AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT"]
        self.assertEqual(config.secret_names(plan, ["plan", "triage"]), sorted([*claude, *azure]))
        self.assertEqual(config.secret_names(plan, ["plan"]), claude)
        gpt = config.with_profile(cfg, "gpt")
        self.assertEqual(config.secret_names([gpt], ["implement"]), azure)
        self.assertEqual(config.secret_names([gpt], ["review"]), claude)


class LaunchTests(unittest.TestCase):
    def launch(self, stage, cfg=None):
        cfg = cfg or sol_config(effort="high")
        spec = agent.stage_spec(stage, cfg, "/home/runner/codex")
        secrets = {
            "AZURE_OPENAI_ENDPOINT": "https://acme.openai.azure.com",
            "AZURE_OPENAI_API_KEY": "azure-key-0123456789",
        }
        return RUNTIMES["codex"].launch(cfg["agents"]["sol"], spec, secrets), spec

    def test_access_levels_map_to_sandboxes(self):
        for stage, sandbox in (
            ("triage", "read-only"),
            ("plan", "read-only"),
            ("review", "read-only"),
            ("implement", "danger-full-access"),
            ("fix", "danger-full-access"),
        ):
            cfg = sol_config(profiles={"a": {"when": "x"}}, default_profile="a")
            argv = self.launch(stage, cfg)[0].argv
            self.assertEqual(argv[argv.index("--sandbox") + 1], sandbox, stage)

    def test_command_line_environment_and_files(self):
        launch, spec = self.launch("implement")
        home = Path("/home/runner/codex")
        self.assertEqual(launch.argv[:2], ["codex", "exec"])
        self.assertEqual(launch.argv[-1], "-")
        for flag in ("--json", "--ephemeral", "--strict-config", "--ignore-rules"):
            self.assertIn(flag, launch.argv)
        self.assertEqual(launch.argv[launch.argv.index("--model") + 1], "gpt-6-1-sol")
        self.assertEqual(
            launch.argv[launch.argv.index("--output-schema") + 1], str(home / "schema.json")
        )
        self.assertIn('model_reasoning_effort="high"', launch.argv)
        self.assertIn('web_search="disabled"', launch.argv)
        exclude = (
            'shell_environment_policy.exclude=["AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT"]'
        )
        self.assertIn(exclude, launch.argv)
        self.assertEqual(
            launch.env, {"AZURE_OPENAI_API_KEY": "azure-key-0123456789", "CODEX_HOME": str(home)}
        )
        self.assertEqual(
            launch.files[home / "config.toml"],
            'model_provider = "nexkit"\n\n'
            "[model_providers.nexkit]\n"
            'name = "Azure OpenAI"\n'
            'base_url = "https://acme.openai.azure.com/openai/v1"\n'
            'env_key = "AZURE_OPENAI_API_KEY"\n'
            'wire_api = "responses"\n',
        )
        schema = json.loads(launch.files[home / "schema.json"])
        self.assertEqual(schema, strict(agent.SCHEMAS["implement"]))
        self.assertFalse(schema["additionalProperties"])
        for text in [*launch.argv, *launch.files.values()]:
            self.assertNotIn("azure-key-0123456789", text)
        # The endpoint is only in the run's own config.toml, never on the command line.
        self.assertFalse(any("acme" in arg for arg in launch.argv))

    def test_install_lets_the_sandbox_use_user_namespaces(self):
        commands, bin_dir = RUNTIMES["codex"].install()
        self.assertEqual(commands[0][:2], ["npm", "install"])
        self.assertIn("--ignore-scripts", commands[0])
        self.assertTrue(commands[0][-1].endswith(f"@{RUNTIMES['codex'].version}"))
        # Only where AppArmor restricts them, as on ubuntu-24.04 runners.
        self.assertEqual(commands[1][:2], ["bash", "-c"])
        self.assertIn("apparmor_restrict_unprivileged_userns=0", commands[1][2])
        self.assertTrue(str(bin_dir).endswith("node_modules/.bin"))

    def test_strict_schemas_close_every_object(self):
        schema = strict(agent.SCHEMAS["review"])
        finding = schema["properties"]["findings"]["items"]
        self.assertFalse(finding["additionalProperties"])
        self.assertEqual(finding["properties"]["severity"]["type"], "string")
        self.assertEqual(schema["required"], list(agent.SCHEMAS["review"]["properties"]))


class ReaderTests(unittest.TestCase):
    def test_a_recorded_run(self):
        shown, outcome = read(RECORDED)
        self.assertEqual(
            [(e.kind, e.name) for e in shown],
            [
                ("notice", ""),
                ("turn", ""),
                ("tool_call", "shell"),
                ("tool_result", ""),
                ("text", ""),
            ],
        )
        self.assertEqual(shown[2].command, "/bin/bash -lc 'ls -1'")
        self.assertEqual((shown[3].text, shown[3].is_error), ("a.txt\n", False))
        self.assertEqual(outcome.output, PLAN)
        self.assertIsNone(outcome.error)
        self.assertIsNone(outcome.cost_usd)
        self.assertEqual(outcome.tokens["input_tokens"], 200)

    def test_failed_commands_and_file_changes(self):
        events = codex_events(DONE, commands=[("make test", "1 failed", 1)])
        change = {"id": "f", "type": "file_change", "status": "completed"}
        change["changes"] = [{"path": "calc.py", "kind": "update"}]
        events.insert(-2, {"type": "item.completed", "item": change})
        shown, _ = read(events)
        results = [e for e in shown if e.kind == "tool_result"]
        self.assertEqual(
            [(e.text, e.is_error) for e in results], [("1 failed", True), ("update calc.py", False)]
        )
        edit = next(e for e in shown if e.name == "edit")
        self.assertEqual(edit.input, {"file_path": "calc.py"})

    def test_failures(self):
        _, outcome = read(FAILED, returncode=1)
        self.assertEqual(outcome.error, f"stopped: {ERROR}")
        _, crashed = read([{"type": "turn.started"}], returncode=2, stderr="boom")
        self.assertEqual(crashed.error, "exited with code 2 without a result. boom")
        _, prose = read(codex_events("I fixed it."))
        self.assertIsNone(prose.output)
        self.assertIsNone(prose.error)

    def test_interpreted_with_its_label(self):
        _, outcome = read(FAILED, returncode=1)
        run = {"seconds": 3.0, "timed_out": False}
        result = agent.interpret("plan", run, outcome, 5, label="Codex")
        self.assertEqual(result["error"], f"Codex stopped: {ERROR}")
        _, prose = read(codex_events("I fixed it."))
        result = agent.interpret("plan", run, prose, 5, label="Codex")
        self.assertEqual(result["error"], "Codex finished without the required structured result.")
        self.assertEqual(result["tokens"]["output_tokens"], 300)
        self.assertIn("| 1,200 in, 300 out |", runlog.summary(result, []))


class StageTests(unittest.TestCase):
    def setUp(self):
        self.repos = GitRepos()
        self.repo = self.repos.clone("work")
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "out"
        self.codex = FakeCodex(self.tmp.name)

    def tearDown(self):
        self.codex.restore()
        self.tmp.cleanup()
        self.repos.cleanup()

    def run_stage(self, stage, cfg=None, **kw):
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            result = agent.run_stage(
                stage,
                CONTEXT,
                cfg or sol_config(),
                self.repo,
                self.out,
                program=str(self.codex.path),
                **kw,
            )
        return result, stdout.getvalue()

    def test_implement_on_codex(self):
        commands = [
            ("/bin/bash -lc 'echo key=$AZURE_OPENAI_API_KEY'", "key=azure-key-0123456789\n", 0)
        ]
        self.codex.configure(events=codex_events(DONE, commands=commands), write={"calc.py": "x"})
        with mock.patch.dict(
            os.environ, {"GITHUB_ACTIONS": "true", "ANTHROPIC_API_KEY": "anthropic-0123456789"}
        ):
            result, stdout = self.run_stage("implement")
        self.assertEqual(result["status"], "done", result.get("error"))
        self.assertEqual(result["changed_files"], ["calc.py"])
        self.assertEqual(result["tokens"]["input_tokens"], 1200)
        call = json.loads(self.codex.log.read_text())
        # Of the agent secrets, only its own; never Claude's.
        agent_secrets = {"CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "AZURE_OPENAI_ENDPOINT"}
        self.assertEqual(
            [k for k in call["env"] if k in agent_secrets | {"AZURE_OPENAI_API_KEY"}],
            ["AZURE_OPENAI_API_KEY"],
        )
        self.assertIn(
            'base_url = "https://acme.openai.azure.com/openai/v1"', call["home"]["config.toml"]
        )
        self.assertEqual(sorted(call["home"]), ["config.toml", "schema.json"])
        self.assertIn("Change add to return a + b.", call["prompt"])
        self.assertEqual(Path(call["cwd"]).resolve(), self.repo.resolve())
        for text in (
            stdout,
            (self.out / "transcript.jsonl").read_text(),
            (self.out / "transcript.md").read_text(),
        ):
            self.assertNotIn("azure-key-0123456789", text)
        self.assertIn("## [00:00 #1] Codex", (self.out / "transcript.md").read_text())

    def test_missing_azure_key(self):
        self.codex.configure(events=codex_events(PLAN))
        env = {"GITHUB_ACTIONS": "true", "AZURE_OPENAI_API_KEY": ""}
        with mock.patch.dict(os.environ, env):
            result, _ = self.run_stage("plan")
        self.assertEqual(
            result["error"],
            "No secret for the `sol` agent: set the AZURE_OPENAI_API_KEY repository secret "
            "and pass it in the `secrets:` of the NexKit workflow.",
        )
        self.assertFalse(self.codex.log.exists())
        # Codex cannot run without an endpoint, so it is missing on a person's machine too.
        with mock.patch.dict(os.environ, {"AZURE_OPENAI_ENDPOINT": ""}):
            result, _ = self.run_stage("plan")
        self.assertIn("set the AZURE_OPENAI_ENDPOINT repository secret", result["error"])

    def test_the_endpoint_secret_is_checked_before_codex_starts(self):
        self.codex.configure(events=codex_events(PLAN))
        for value, message in (
            ("https://evil.example.com", "the host must end with .openai.azure.com"),
            ("http://acme.openai.azure.com", "must be an https URL"),
            ("https://acme.openai.azure.com/openai/v1", "only a host"),
        ):
            with (
                self.subTest(value=value),
                mock.patch.dict(os.environ, {"AZURE_OPENAI_ENDPOINT": value}),
            ):
                result, _ = self.run_stage("plan")
                self.assertTrue(result["error"].startswith("The AZURE_OPENAI_ENDPOINT secret"))
                self.assertIn(message, result["error"])
                self.assertTrue(result["error"].endswith("(the `sol` agent)."))
                self.assertNotIn(value, result["error"])
        self.assertFalse(self.codex.log.exists())

    def test_a_result_that_does_not_fit_publishes_nothing(self):
        wrong = {**DONE, "status": "finished"}
        self.codex.configure(events=codex_events(wrong), write={"calc.py": "x"})
        result, _ = self.run_stage("implement")
        self.assertEqual(result["status"], "error")
        self.assertIn("result.status must be one of 'done', 'blocked'", result["error"])
        self.assertFalse((self.out / "changes.patch").exists())

    def test_the_command_list_uses_shell_commands(self):
        self.codex.configure(
            events=codex_events(DONE, commands=[("make test", "ok", 0)]), write={"calc.py": "x"}
        )
        self.run_stage("implement")
        self.assertIn("✓ make test", runlog.summary({"stage": "implement"}, self.calls()))

    def calls(self):
        log = runlog.RunLog(Redactor(), reader=CodexReader())
        with contextlib.redirect_stdout(io.StringIO()):
            for line in (self.out / "transcript.jsonl").read_text().splitlines():
                log.line(line)
            log.close()
        return log.calls


if __name__ == "__main__":
    unittest.main()
