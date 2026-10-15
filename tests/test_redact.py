import base64
import unittest
from urllib.parse import quote

from nexkit.redact import Redactor

ENV = {
    "CLAUDE_CODE_OAUTH_TOKEN": "oauth-value-1234567890",
    "NEXKIT_PUSH_TOKEN": "push-value-abcdefgh",
    "MY_SERVICE_SECRET": "service-secret-value",
    "Db_Password": "hunter2hunter2",
    "SHORT_TOKEN": "abc123",
    "HOME": "/home/runner",
}


class RedactTests(unittest.TestCase):
    def setUp(self):
        self.redact = Redactor(ENV)

    def assertRedacted(self, text, secret):
        redacted = self.redact(text)
        self.assertNotIn(secret, redacted)
        self.assertIn("***", redacted)
        return redacted

    def test_environment_values(self):
        for value in ("oauth-value-1234567890", "push-value-abcdefgh", "service-secret-value"):
            with self.subTest(value=value):
                self.assertEqual(self.redact(f"x={value}!"), "x=***!")
        self.assertEqual(self.redact("hunter2hunter2"), "***")  # name matched in any case
        self.assertEqual(self.redact("abc123 /home/runner"), "abc123 /home/runner")

    def test_github_token_and_credentials_are_always_secrets(self):
        redact = Redactor({"GITHUB_TOKEN": "ghs-not-a-real-shape", "ANTHROPIC_API_KEY": "k" * 12})
        self.assertEqual(redact("ghs-not-a-real-shape kkkkkkkkkkkk"), "*** ***")
        # An Azure key has no known shape; it is redacted by name.
        azure = Redactor({"AZURE_OPENAI_API_KEY": "0123456789abcdef0123456789abcdef"})
        self.assertEqual(azure("api-key: 0123456789abcdef0123456789abcdef"), "api-key: ***")
        # Agent secrets go by the names the configuration gives them.
        endpoint = "https://acme.openai.azure.com"
        named = Redactor(
            {"NEXKIT_AGENT_SECRETS": '["AZURE_OPENAI_ENDPOINT"]', "AZURE_OPENAI_ENDPOINT": endpoint}
        )
        self.assertEqual(named(f"url: {endpoint}/openai/v1"), "url: ***/openai/v1")
        # And the values an agent runs with, whatever their names.
        local = Redactor({})
        local.add(["https://other.openai.azure.com", "deployment-key-1234", "", "short"])
        self.assertEqual(
            local("https://other.openai.azure.com deployment-key-1234 short"), "*** *** short"
        )

    def test_encoded_environment_values(self):
        value = "oauth-value-1234567890"
        encoded = base64.b64encode(value.encode()).decode()
        echoed = base64.b64encode(f"{value}\n".encode()).decode()
        self.assertRedacted(f"echo: {encoded}", encoded.rstrip("="))
        self.assertRedacted(f"echo: {echoed}", echoed.rstrip("="))
        redact = Redactor({"API_KEY": "p@ss word/1"})
        self.assertEqual(redact(f"?key={quote('p@ss word/1', safe='')}"), "?key=***")

    def test_token_shapes(self):
        cases = {
            "anthropic": "sk-ant-api03-AbCdEf_123-456",
            "openai project": "sk-proj-" + "Ab1_" * 12,
            "openai service account": "sk-svcacct-" + "Zx9-" * 10,
            "openai": "sk-" + "a1B2c3D4" * 4,
            "ghp": "ghp_" + "a1B2" * 9,
            "gho": "gho_" + "Z9" * 18,
            "ghu": "ghu_" + "x" * 36,
            "ghs": "ghs_" + "y" * 36,
            "ghr": "ghr_" + "z" * 36,
            "fine-grained": "github_pat_11ABCDEFG0_" + "q" * 40,
            "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n",
        }
        for name, secret in cases.items():
            with self.subTest(name=name):
                self.assertEqual(self.redact(f"value {secret} end"), "value *** end")

    def test_authorization_headers(self):
        text = self.assertRedacted("curl -H 'Authorization: Bearer abc.def-123456'", "abc.def")
        self.assertEqual(text, "curl -H 'Authorization: Bearer ***'")
        text = self.assertRedacted('{"authorization": "token opaque-value-99"}', "opaque")
        self.assertEqual(text, '{"authorization": "token ***"}')
        self.assertEqual(self.redact("bearer zzzzzzzzzzzz"), "bearer ***")

    def test_private_key_blocks(self):
        key = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nIBAAK\n-----END RSA PRIVATE KEY-----"
        self.assertEqual(self.redact(f"key:\n{key}\ndone"), "key:\n***\ndone")
        key = key.replace("RSA ", "")
        self.assertEqual(self.redact(key), "***")

    def test_connection_string_passwords(self):
        cases = [
            ("Server=db;User Id=sa;Password=S3cret!;", "Server=db;User Id=sa;Password=***;"),
            ("host=db PWD=abc dbname=x", "host=db PWD=*** dbname=x"),
            ("export DB_PASSWORD='two words'", "export DB_PASSWORD=***"),
            ("postgres://app:s3cret@db:5432/app", "postgres://app:***@db:5432/app"),
            (
                "https://x-access-token:opaque@github.com/a/b",
                "https://x-access-token:***@github.com/a/b",
            ),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(self.redact(text), expected)

    def test_ordinary_text_is_unchanged(self):
        text = "\n".join(
            [
                "commit 3f786850e387550fdab836ed7e6dc881de23001b",
                "id 123e4567-e89b-12d3-a456-426614174000",
                "sha256 e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                "OLDPWD=/home/runner/work https://github.com/acme/app/pull/3",
                "password: see the docs; the key is in the vault",
                "assertEqual(add(2, 3), 5)",
            ]
        )
        self.assertEqual(self.redact(text), text)

    def test_data_redacts_every_string(self):
        data = {"a": ["push-value-abcdefgh", 3, None], "push-value-abcdefgh": {"b": True}}
        self.assertEqual(self.redact.data(data), {"a": ["***", 3, None], "***": {"b": True}})


if __name__ == "__main__":
    unittest.main()
