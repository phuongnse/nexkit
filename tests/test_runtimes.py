import json
import unittest

from nexkit import config
from nexkit.runtimes import (
    DECLARED_SECRETS,
    NAMES_VAR,
    PROVIDERS,
    RUNTIMES,
    SECRET_SLOTS,
    SLOT_VAR,
    secret_names,
    unpack_secrets,
)
from nexkit.runtimes.base import AgentConfigError, Field, reserved
from tests.support import CLAUDE, OTHER, TEAM, make_config, with_other_runtime


class RegistryTests(unittest.TestCase):
    def test_every_runtime_supports_known_providers_and_every_stage(self):
        for runtime in RUNTIMES.values():
            self.assertTrue(runtime.label and runtime.version and runtime.program, runtime.name)
            self.assertLessEqual(set(runtime.providers), set(PROVIDERS), runtime.name)

    def test_secret_fields_end_in_secret(self):
        for provider in PROVIDERS.values():
            for key, field in provider.fields.items():
                if field.kind == "secret":
                    self.assertTrue(key.endswith("_secret"), key)

    def test_reserved_names(self):
        for name in (
            "GITHUB_TOKEN",
            "GITHUB_ANYTHING",
            "NEXKIT_PUSH_TOKEN",
            "NEXKIT_AGENT_SECRET_1",
        ):
            self.assertTrue(reserved(name), name)
        self.assertFalse(reserved("GPT_SOL_KEY"))

    def test_the_built_in_agent_needs_no_settings(self):
        cfg = config.validate({"agents": CLAUDE})
        agent = config.agent(cfg, "claude")
        self.assertEqual(
            PROVIDERS[agent["provider"]].secrets(agent), [("CLAUDE_CODE_OAUTH_TOKEN",)]
        )


class AnthropicTests(unittest.TestCase):
    def test_claude_code_gets_the_secret_under_its_own_variable(self):
        from nexkit.agent import stage_spec

        agent = {"runtime": "claude-code", "provider": "anthropic", "api_key_secret": "TEAM_KEY"}
        cfg = config.validate({"agents": {"claude": agent}})
        spec = stage_spec("plan", cfg, "/nonexistent")
        launch = RUNTIMES["claude-code"].launch(agent, spec, {"TEAM_KEY": "sk-ant-x"})
        self.assertEqual(
            launch.env,
            {"ANTHROPIC_API_KEY": "sk-ant-x", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"},
        )
        self.assertEqual(PROVIDERS["anthropic"].secrets(agent), [("TEAM_KEY",)])


class RemovedTests(unittest.TestCase):
    CODEX = {
        "runtime": "codex",
        "provider": "azure-openai",
        "endpoint_secret": "AZURE_OPENAI_ENDPOINT",
        "api_key_secret": "GPT_ASTRA_KEY",
    }

    def test_a_removed_runtime_says_what_to_do(self):
        raw = {"agents": {**CLAUDE, "astra": self.CODEX}}
        with self.assertRaisesRegex(
            config.ConfigError,
            "agents.astra.runtime must be one of claude-code: NexKit 4.0.0 removed the codex "
            "runtime; run the stage on a claude-code agent",
        ):
            config.validate(raw)

    def test_a_removed_provider_says_so(self):
        agent = {**TEAM, "provider": "azure-openai"}
        with self.assertRaisesRegex(
            config.ConfigError, "agents.team.provider: NexKit 4.0.0 removed the azure-openai"
        ):
            config.validate({"agents": {**CLAUDE, "team": agent}})


class MultiAgentTests(unittest.TestCase):
    """Several agents, each with its own secret: each job gets only the secrets it needs."""

    RAW = {
        "agents": {**CLAUDE, "team": TEAM},
        "stages": {"triage": {"agent": "team", "model": "haiku"}},
        "default_profile": "claude",
        "profiles": {
            "claude": {
                "when": "x",
                "stages": {"implement": {"model": "opus", "effort": "max", "max_budget_usd": 5}},
            },
            "team": {"when": "y", "stages": {"implement": {"agent": "team", "model": "sonnet"}}},
        },
    }

    def test_jobs_validate_the_layered_configuration_again(self):
        cfg = config.validate(self.RAW)
        for name in cfg["profiles"]:
            layered = config.with_profile(cfg, name)
            again = config.validate(json.loads(json.dumps(layered)), layered=True)
            self.assertEqual(again, layered, name)

    def test_each_job_gets_only_the_secrets_its_stages_need(self):
        cfg = config.validate(self.RAW)
        plan = [cfg, *(config.with_profile(cfg, name) for name in cfg["profiles"])]
        claude, team = ["CLAUDE_CODE_OAUTH_TOKEN"], ["TEAM_KEY"]
        self.assertEqual(config.secret_names(plan, ["plan", "triage"]), sorted([*claude, *team]))
        self.assertEqual(config.secret_names(plan, ["plan"]), claude)
        other = config.with_profile(cfg, "team")
        self.assertEqual(config.secret_names([other], ["implement"]), team)
        self.assertEqual(config.secret_names([other], ["review"]), claude)

    def test_one_job_holds_at_most_the_slots(self):
        agents = {f"a{n}": {**TEAM, "api_key_secret": f"KEY_{n}"} for n in range(8)}
        profiles = {
            name: {"when": "x", "stages": {"plan": {"agent": name, "model": "m"}}}
            for name in agents
        }
        with self.assertRaisesRegex(config.ConfigError, "The plan stage can need 9 agent secrets"):
            raw = {"agents": {**CLAUDE, **agents}, "profiles": profiles, "default_profile": "a0"}
            config.validate(raw)

    def test_stages_may_run_on_another_runtime(self):
        with with_other_runtime():
            cfg = make_config(
                agents={"other": OTHER}, stages={"review": {"agent": "other", "model": "m"}}
            )
            self.assertEqual(config.runtimes(cfg), ["claude-code", "other"])
            self.assertEqual(config.secret_names([cfg], ["review"]), ["OTHER_KEY"])
            with self.assertRaisesRegex(config.ConfigError, "max_budget_usd: Other has no"):
                make_config(
                    agents={"other": OTHER},
                    stages={"review": {"agent": "other", "model": "m", "max_budget_usd": 1}},
                )


class SlotTests(unittest.TestCase):
    def test_unpack_moves_slots_to_their_names(self):
        names = ["TEAM_GATEWAY", "TEAM_KEY", "UNSET_ONE"]
        environ = {NAMES_VAR: json.dumps(names), "KEEP": "1"}
        environ.update({SLOT_VAR % i: "" for i in range(1, SECRET_SLOTS + 1)})
        environ.update({SLOT_VAR % 1: "https://gateway.example.com", SLOT_VAR % 2: "key"})
        unpack_secrets(environ)
        self.assertEqual(
            environ,
            {
                NAMES_VAR: json.dumps(names),
                "KEEP": "1",
                "TEAM_GATEWAY": "https://gateway.example.com",
                "TEAM_KEY": "key",
            },
        )
        self.assertEqual(secret_names(environ), {*DECLARED_SECRETS, *names})

    def test_reserved_names_are_never_unpacked(self):
        environ = {NAMES_VAR: '["NEXKIT_PUSH_TOKEN"]', SLOT_VAR % 1: "token"}
        unpack_secrets(environ)
        self.assertNotIn("NEXKIT_PUSH_TOKEN", environ)


class FieldTests(unittest.TestCase):
    def check(self, field, value):
        field.check(value, "agents.x.key")

    def test_secret_fields_hold_an_allowlisted_name(self):
        secret = Field("secret")
        self.check(secret, "ANTHROPIC_API_KEY")
        self.check(secret, "GPT_SOL_KEY")
        with self.assertRaisesRegex(AgentConfigError, "the config holds only its name"):
            self.check(secret, "sk-ant-api03-abcdefghijklmnop")
        for name in ("GITHUB_TOKEN", "NEXKIT_PUSH_TOKEN", "NEXKIT_FUTURE"):
            with self.assertRaisesRegex(AgentConfigError, "reserved for GitHub and NexKit"):
                self.check(secret, name)

    def test_url_fields(self):
        url = Field("url", hosts=(".gateway.example.net", "example.com"))
        for good in ("https://acme.gateway.example.net", "https://example.com/"):
            self.check(url, good)
        bad = [
            "http://acme.gateway.example.net",
            "https://user:pw@acme.gateway.example.net",
            "https://acme.gateway.example.net/openai",
            "https://acme.gateway.example.net?x=1",
            "https://acme.gateway.example.net#x",
            "https://evil.com",
            "https://gateway.example.net.evil.com",
            "https://sub.example.com",
            None,
        ]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(AgentConfigError):
                self.check(url, value)

    def test_string_fields(self):
        with self.assertRaisesRegex(AgentConfigError, "non-empty string"):
            self.check(Field("str"), " ")


if __name__ == "__main__":
    unittest.main()
