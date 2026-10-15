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
from tests.support import CLAUDE


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
        self.assertEqual(launch.env, {"ANTHROPIC_API_KEY": "sk-ant-x"})
        self.assertEqual(PROVIDERS["anthropic"].secrets(agent), [("TEAM_KEY",)])


class SlotTests(unittest.TestCase):
    def test_unpack_moves_slots_to_their_names(self):
        names = ["AZURE_OPENAI_ENDPOINT", "GPT_SOL_KEY", "UNSET_ONE"]
        environ = {NAMES_VAR: json.dumps(names), "KEEP": "1"}
        environ.update({SLOT_VAR % i: "" for i in range(1, SECRET_SLOTS + 1)})
        environ.update({SLOT_VAR % 1: "https://acme.openai.azure.com", SLOT_VAR % 2: "key"})
        unpack_secrets(environ)
        self.assertEqual(
            environ,
            {
                NAMES_VAR: json.dumps(names),
                "KEEP": "1",
                "AZURE_OPENAI_ENDPOINT": "https://acme.openai.azure.com",
                "GPT_SOL_KEY": "key",
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
        url = Field("url", hosts=(".openai.azure.com", "example.com"))
        for good in ("https://acme.openai.azure.com", "https://example.com/"):
            self.check(url, good)
        bad = [
            "http://acme.openai.azure.com",
            "https://user:pw@acme.openai.azure.com",
            "https://acme.openai.azure.com/openai",
            "https://acme.openai.azure.com?x=1",
            "https://acme.openai.azure.com#x",
            "https://evil.com",
            "https://openai.azure.com.evil.com",
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
