"""Agent runtimes and model providers, and the only places that list them.

An agent is a runtime paired with a provider and the provider's settings. Stages choose
an agent by name; `claude` is built in.
"""

from __future__ import annotations

from .claude_code import ClaudeCode
from .providers import Anthropic

RUNTIMES = {runtime.name: runtime for runtime in (ClaudeCode(),)}
PROVIDERS = {provider.name: provider for provider in (Anthropic(),)}
# Every secret an agent may use. pipeline.yml declares exactly these and passes them only to
# the steps that run an agent (tests/test_cli.py checks both).
AGENT_SECRETS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
BUILTIN = {"claude": {"runtime": "claude-code", "provider": "anthropic"}}
