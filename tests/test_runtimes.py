import unittest

from nexkit import config
from nexkit.runtimes import AGENT_SECRETS, PROVIDERS, RUNTIMES
from nexkit.runtimes.base import AgentConfigError, Field


class RegistryTests(unittest.TestCase):
    def test_every_runtime_supports_known_providers_and_every_stage(self):
        for runtime in RUNTIMES.values():
            self.assertTrue(runtime.label and runtime.version and runtime.program, runtime.name)
            self.assertLessEqual(set(runtime.providers), set(PROVIDERS), runtime.name)

    def test_provider_secrets_are_allowlisted(self):
        for provider in PROVIDERS.values():
            for key, field in provider.fields.items():
                if field.kind == "secret":
                    self.assertTrue(key.endswith("_secret"), key)
        self.assertEqual(len(set(AGENT_SECRETS)), len(AGENT_SECRETS))
        self.assertNotIn("GITHUB_TOKEN", AGENT_SECRETS)
        self.assertNotIn("NEXKIT_PUSH_TOKEN", AGENT_SECRETS)

    def test_the_built_in_agent_needs_no_settings(self):
        cfg = config.validate({})
        agent = config.agent(cfg, "claude")
        self.assertEqual(PROVIDERS[agent["provider"]].credentials(agent), AGENT_SECRETS[:2])


class FieldTests(unittest.TestCase):
    def check(self, field, value):
        field.check(value, "agents.x.key", AGENT_SECRETS)

    def test_secret_fields_hold_an_allowlisted_name(self):
        secret = Field("secret")
        self.check(secret, "ANTHROPIC_API_KEY")
        with self.assertRaisesRegex(AgentConfigError, "the config holds only its name"):
            self.check(secret, "sk-ant-api03-abcdefghijklmnop")
        with self.assertRaisesRegex(AgentConfigError, "NexKit passes only these secrets"):
            self.check(secret, "GITHUB_TOKEN")

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
