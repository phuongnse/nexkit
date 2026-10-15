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
# A configuration names its agents' secrets freely, except names with these prefixes: they
# belong to GitHub and to NexKit itself, such as NEXKIT_PUSH_TOKEN and the pipeline's slots.
RESERVED_PREFIXES = ("GITHUB_", "NEXKIT_")


def reserved(name):
    return name.startswith(RESERVED_PREFIXES)


# The keys of `Outcome.tokens`, the same for every runtime. `input` counts every input token,
# `cached` the part of them read from a cache, and `output` every output token, reasoning too.
TOKENS = ("input", "cached", "output")


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
    `files` are written into the run's home before the start and never hold a key."""

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
    # TOKENS: what every model the run used read and wrote, or None when the CLI did not say.
    tokens: dict | None = None
    # {"message": the line that says so, "reset": UTC datetime or None} when the CLI said it
    # stopped at a usage limit that resets. The run then pauses instead of failing.
    limit: dict | None = None


@dataclass(frozen=True)
class Field:
    """A provider setting. A `secret` field holds the name of a secret, never its value;
    `holds` describes the value, which only the job that runs the agent can check."""

    kind: str  # "str", "url" or "secret"
    required: bool = True
    # For URLs: the host must be one of these or end with one that starts with ".".
    hosts: tuple[str, ...] = ()
    holds: Field | None = None

    def check(self, value, where):
        if self.kind == "str":
            if not isinstance(value, str) or not value.strip():
                raise AgentConfigError(f"{where} must be a non-empty string")
        elif self.kind == "secret":
            if not isinstance(value, str) or not SECRET_NAME.match(value):
                raise AgentConfigError(
                    f"{where} must be the name of a repository secret. Put the key in a "
                    "repository secret; the config holds only its name"
                )
            if reserved(value):
                raise AgentConfigError(
                    f"{where}: names that start with {' or '.join(RESERVED_PREFIXES)} are "
                    "reserved for GitHub and NexKit; NexKit never passes them to agents"
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
    # Whether the CLI can run without the secrets on a person's machine, with its own login.
    local_login = False

    def check(self, agent, where):
        """Raise AgentConfigError for settings that do not fit together."""

    def secrets(self, agent):
        """The secrets the agent needs, as groups: one secret of every group must be set."""
        return [
            (agent[key],) for key, f in self.fields.items() if f.kind == "secret" and key in agent
        ]

    def check_secrets(self, agent, values):
        """Raise AgentConfigError for a secret whose value this provider cannot use. The
        message names the secret, never its value."""
        for key, f in self.fields.items():
            name = agent.get(key)
            if f.kind == "secret" and f.holds and values.get(name):
                f.holds.check(values[name], f"The {name} secret")

    def secret_hint(self, names, repo):
        """How a person sets one of `names`, for `nexkit doctor`."""
        return f"run 'gh secret set {names[0]} --repo {repo}'"


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
    # Whether the reader can tell that the CLI stopped at a usage limit (`Outcome.limit`).
    # Only then can a run pause, or move to the agent's fallback.
    reports_limits = False
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
