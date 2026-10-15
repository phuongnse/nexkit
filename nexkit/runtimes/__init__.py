"""Agent runtimes and model providers, and the only places that list them.

An agent is a runtime paired with a provider and the provider's settings. Stages choose
an agent by name; `claude` is built in.
"""

from __future__ import annotations

import json
import os

from .base import reserved
from .claude_code import CREDENTIALS, ClaudeCode
from .codex import Codex
from .providers import Anthropic, AzureOpenAI

RUNTIMES = {runtime.name: runtime for runtime in (ClaudeCode(), Codex())}
PROVIDERS = {provider.name: provider for provider in (Anthropic(), AzureOpenAI())}
BUILTIN = {"claude": {"runtime": "claude-code", "provider": "anthropic"}}
# The secrets the built-in agent may use; the pipeline still declares them, so a workflow
# that passes them one by one keeps working.
DECLARED_SECRETS = CREDENTIALS
# route names the secrets a job's stages need in NAMES_VAR, and pipeline.yml reads each
# one by name into a numbered slot: SLOT_VAR % 1 … SLOT_VAR % SECRET_SLOTS.
NAMES_VAR = "NEXKIT_AGENT_SECRETS"
SLOT_VAR = "NEXKIT_AGENT_SECRET_%d"
SECRET_SLOTS = 8


def unpack_secrets(environ=os.environ):
    """Move the job's agent secrets from the pipeline's slots to their own names, so the
    rest of NexKit reads them like locally set variables. Unset ones stay unset."""
    names = json.loads(environ.get(NAMES_VAR) or "[]")
    for index in range(1, SECRET_SLOTS + 1):
        value = environ.pop(SLOT_VAR % index, "")
        if value and index <= len(names) and not reserved(names[index - 1]):
            environ[names[index - 1]] = value


def secret_names(environ=os.environ):
    """The names of every agent secret this job may hold."""
    names = json.loads(environ.get(NAMES_VAR) or "[]")
    return {*CREDENTIALS, *names}
