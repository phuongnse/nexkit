"""What NexKit needs from an agent runtime and a model provider.

A runtime is a CLI agent that NexKit starts as a process, such as Claude Code. A provider
is where the runtime's model is served and how it authenticates. An agent pairs one of
each. NexKit's own code only uses these types; everything specific to one CLI lives in
that runtime's module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

# What a stage may affect. Every runtime supports every level, in its own way.
ACCESS = {
    "triage": "none",  # no repository: an empty working directory
    "plan": "read",
    "review": "read",
    "implement": "write",  # anything on the disposable runner
    "fix": "write",
}
SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


class AgentConfigError(ValueError):
    """An agent setting that the runtime or provider cannot honour."""


@dataclass(frozen=True)
class Spec:
    """What a stage needs from the runtime, never how the CLI spells it."""

    stage: str
    model: str
    effort: str | None
    budget_usd: float | None
    schema: dict
    # An empty directory for the CLI's own configuration and state, removed afterwards.
    home: Path

    @property
    def access(self):
        return ACCESS[self.stage]


@dataclass(frozen=True)
class Launch:
    """How to start the CLI. `env` is added to an environment without any agent secret;
    `files` are written before the start and never hold a secret."""

    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)
    files: dict[Path, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Event:
    """One thing to show in the log and transcript.

    `turn` starts a new model turn, `text` is the agent's own words, `tool_call` and
    `tool_result` share an `id`, and `notice` is something the CLI itself said."""

    kind: str
    id: str | None = None
    name: str = ""
    input: Any = None
    text: str = ""
    is_error: bool | None = False
    # The shell command of a tool call, for the run summary's list of commands.
    command: str | None = None


@dataclass
class Outcome:
    """The end of a run as the CLI reported it. NexKit validates `output` itself.

    `error` is what the CLI said about a failure, phrased to follow the runtime's label:
    "exited with code 1 without a result." It is never the model's own text."""

    output: Any = None
    error: str | None = None
    cost_usd: float | None = None
    tokens: dict | None = None
    turns: int | None = None
    # {"message": the line that says so, "reset": UTC datetime or None} at a usage limit.
    limit: dict | None = None


@dataclass(frozen=True)
class Field:
    """A provider setting. A `secret` field holds the name of a secret, never its value."""

    kind: str  # "str", "url" or "secret"
    required: bool = True
    # For URLs: the host must be one of these or end with one that starts with ".".
    hosts: tuple[str, ...] = ()

    def check(self, value, where, secrets):
        if self.kind == "str":
            if not isinstance(value, str) or not value.strip():
                raise AgentConfigError(f"{where} must be a non-empty string")
        elif self.kind == "secret":
            if not isinstance(value, str) or not SECRET_NAME.match(value):
                raise AgentConfigError(
                    f"{where} must be the name of a repository secret. Put the key in a "
                    "repository secret; the config holds only its name"
                )
            if value not in secrets:
                raise AgentConfigError(
                    f"{where}: NexKit passes only these secrets to agents: {', '.join(secrets)}"
                )
        elif self.kind == "url":
            self._url(value, where)

    def _url(self, value, where):
        parts = urlsplit(value) if isinstance(value, str) else None
        if (
            not parts
            or parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or parts.path not in ("", "/")
        ):
            raise AgentConfigError(
                f"{where} must be an https URL with only a host, such as https://example.com"
            )
        host = parts.hostname.lower()
        if self.hosts and not any(
            host.endswith(allowed) if allowed.startswith(".") else host == allowed
            for allowed in self.hosts
        ):
            raise AgentConfigError(f"{where}: the host must end with {' or '.join(self.hosts)}")


class Provider:
    """Where a model is served. Subclasses set `name` and `fields`."""

    name = ""
    fields: dict[str, Field] = {}

    def credentials(self, agent):
        """The secrets that authenticate this agent. Any one of them is enough."""
        return tuple(agent[key] for key, f in self.fields.items() if f.kind == "secret")

    def secret_hint(self, agent, repo):
        """How a person sets the agent's secret, for `nexkit doctor`."""
        return f"run 'gh secret set {self.credentials(agent)[0]} --repo {repo}'"


class Reader:
    """Reads one run's output: an event for the log per line, then the outcome."""

    def events(self, raw):
        """The events to show for one parsed line of output."""
        raise NotImplementedError

    def outcome(self, returncode, stderr):
        raise NotImplementedError


class Runtime:
    """A CLI agent. Subclasses set the attributes and implement the methods."""

    name = ""
    label = ""  # how logs and comments name it: "Claude did not finish …"
    version = ""  # pinned in nexkit/__init__.py
    pin = ""  # the name of that constant
    # Where the newest stable version is published, and how to read it from the response.
    release_url = ""
    program = ""  # the executable, found on PATH
    efforts: tuple[str, ...] = ()
    supports_budget = False
    # Provider name -> function(agent, spec, secrets) returning the Launch parts for it.
    providers: dict = {}

    def release_version(self, text):
        return text.strip()

    def install(self):
        """Commands that install the pinned version, and the directory to add to PATH."""
        raise NotImplementedError

    def validate(self, settings, where):
        """Raise AgentConfigError for a stage setting this runtime cannot honour."""
        effort = settings.get("effort")
        if effort is not None and effort not in self.efforts:
            raise AgentConfigError(
                f"{where}.effort must be one of {', '.join(self.efforts)} or null for {self.label}"
            )
        if settings.get("max_budget_usd") is not None and not self.supports_budget:
            raise AgentConfigError(
                f"{where}.max_budget_usd: {self.label} has no spending limit; remove it and "
                "rely on timeout_minutes"
            )

    def launch(self, agent, spec, secrets, program=None):
        raise NotImplementedError

    def connect(self, agent, spec, secrets):
        """The provider's part of the launch."""
        return self.providers[agent["provider"]](agent, spec, secrets)

    def reader(self):
        raise NotImplementedError
