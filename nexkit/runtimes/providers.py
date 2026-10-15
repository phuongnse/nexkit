"""Where models are served. A provider declares its settings and which of them are secrets."""

from __future__ import annotations

from .base import Field, Provider
from .claude_code import CREDENTIALS


class Anthropic(Provider):
    """Claude's own service: a subscription token or an API key, whichever is set."""

    name = "anthropic"
    fields = {}
    # Claude Code uses the person's own login when no secret is set.
    local_login = True

    def secrets(self, agent):
        return [CREDENTIALS]

    def secret_hint(self, names, repo):
        return (
            "run 'claude setup-token' and "
            f"'gh secret set CLAUDE_CODE_OAUTH_TOKEN --repo {repo}', or set ANTHROPIC_API_KEY"
        )


AZURE_HOSTS = (".openai.azure.com", ".services.ai.azure.com", ".cognitiveservices.azure.com")


class AzureOpenAI(Provider):
    """OpenAI models deployed in an Azure AI Foundry resource, reached through the v1 API
    with a key. The model of a stage is the deployment name. The resource's endpoint is
    personal, so it lives in a secret like the key, never in the configuration."""

    name = "azure-openai"
    fields = {
        "endpoint_secret": Field("secret", holds=Field("url", hosts=AZURE_HOSTS)),
        "api_key_secret": Field("secret"),
    }
