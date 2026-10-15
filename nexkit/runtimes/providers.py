"""Where models are served. A provider declares its settings and which of them are secrets."""

from __future__ import annotations

from .base import Field, Provider
from .claude_code import CREDENTIALS


class Anthropic(Provider):
    """Claude's own service: a subscription token or an API key, whichever is set."""

    name = "anthropic"
    fields = {}

    def credentials(self, agent):
        return CREDENTIALS

    def secret_hint(self, agent, repo):
        return (
            "run 'claude setup-token' and "
            f"'gh secret set CLAUDE_CODE_OAUTH_TOKEN --repo {repo}', or set ANTHROPIC_API_KEY"
        )


class AzureOpenAI(Provider):
    """OpenAI models deployed in Azure AI Foundry, reached through the v1 API with a key.
    The model of a stage is the deployment name."""

    name = "azure-openai"
    fields = {
        "endpoint": Field(
            "url",
            hosts=(".openai.azure.com", ".services.ai.azure.com", ".cognitiveservices.azure.com"),
        ),
        "api_key_secret": Field("secret"),
    }
