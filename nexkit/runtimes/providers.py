"""Where models are served. A provider declares its settings and which of them are secrets."""

from __future__ import annotations

from .base import AgentConfigError, Field, Provider


class Anthropic(Provider):
    """Claude's own service, with a subscription token (`claude setup-token`) or an API key.
    The agent names the one secret it uses."""

    name = "anthropic"
    fields = {
        "oauth_token_secret": Field("secret", required=False),
        "api_key_secret": Field("secret", required=False),
    }
    # Claude Code uses the person's own login when no secret is set.
    local_login = True

    def check(self, agent, where):
        if ("oauth_token_secret" in agent) == ("api_key_secret" in agent):
            raise AgentConfigError(
                f"{where} needs exactly one of oauth_token_secret (a Claude subscription token "
                "from 'claude setup-token') and api_key_secret (an Anthropic API key)"
            )

    def secret_hint(self, names, repo):
        hint = f"run 'gh secret set {names[0]} --repo {repo}'"
        if "oauth" in names[0].lower():
            hint += " with the token from 'claude setup-token'"
        return hint
