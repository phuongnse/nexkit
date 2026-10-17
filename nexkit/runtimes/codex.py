"""Codex CLI: `codex exec` with JSONL events and an output schema for the result.

NexKit gives each run an empty CODEX_HOME holding only the provider's `config.toml`, so no
user configuration, login or session history applies. Project `.rules` files are ignored,
so a repository cannot widen what a read-only stage may run.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .. import CODEX
from ..schema import strict
from .base import Event, Launch, Outcome, Reader, Runtime

PACKAGE = "@openai/codex"
# Codex's Linux sandbox needs unprivileged user namespaces, which Ubuntu 24.04 restricts
# through AppArmor. Without them a read-only stage cannot run even `ls`. The runner is
# disposable and has passwordless sudo, so the install lifts the restriction there.
USER_NAMESPACES = (
    'if [ "$(sysctl -n kernel.apparmor_restrict_unprivileged_userns 2>/dev/null)" = 1 ]; '
    "then sudo -n sysctl -w kernel.apparmor_restrict_unprivileged_userns=0; fi"
)
INSTALL_DIR = Path.home() / ".nexkit" / "codex"
SANDBOX = {"none": "read-only", "read": "read-only", "write": "danger-full-access"}
# Codex's provider id in the generated config.toml.
PROVIDER_ID = "nexkit"
# What Codex says when the provider refuses requests for a rate or quota limit, for example
# "rate limit exceeded: Your requests to <deployment> … have exceeded token rate limit." or
# "exceeded retry limit, last status: 429 Too Many Requests".
RATE_LIMIT = re.compile(r"rate limit|too many requests|\b429\b", re.IGNORECASE)
QUOTA = re.compile(r"quota exceeded|insufficient_quota|exceeded your current quota", re.IGNORECASE)
# A credential, endpoint or deployment problem: waiting never fixes it.
NOT_A_LIMIT = re.compile(r"\b(?:401|403|404)\b|unauthori[sz]ed|forbidden", re.IGNORECASE)
# "Please retry after 60 seconds.", "Please try again in 1.5s."
WAIT = re.compile(
    r"(?:try again|retry) (?:in|after)\s*(\d+(?:\.\d+)?)\s*"
    r"(ms|milliseconds?|s|secs?|seconds?|mins?|minutes?)\b",
    re.IGNORECASE,
)
# Azure counts rate limits per minute, so one that names no wait has passed a minute later.
RATE_WINDOW = timedelta(minutes=1)


def limit_reached(texts, now=None):
    """When one of `texts`, which Codex itself wrote, says the provider refused requests for
    a rate or quota limit: the line that says so and when to try again (UTC), or None for
    that time when a quota names none. Otherwise None."""
    now = now or datetime.now(UTC)
    for text in texts:
        text = str(text or "").strip()
        line = text.splitlines()[0][:300] if text else ""
        rate, quota = RATE_LIMIT.search(text), QUOTA.search(text)
        if not (rate or quota) or NOT_A_LIMIT.search(text):
            continue
        when = None
        wait = WAIT.search(text)
        if wait:
            unit = wait.group(2).lower()
            scale = 0.001 if unit.startswith(("ms", "milli")) else 60 if unit[0] == "m" else 1
            when = now + timedelta(seconds=float(wait.group(1)) * scale)
        elif rate and not quota:
            when = now + RATE_WINDOW
        return {"message": line, "reset": when}
    return None


def azure_openai(agent, spec, secrets):
    """Azure AI Foundry's OpenAI v1 API. Codex reads the key from the variable `env_key`
    names, so the key itself is never in the file. The endpoint is: the file lives in the
    run's own CODEX_HOME, which is removed afterwards and never uploaded."""
    base_url = secrets[agent["endpoint_secret"]].rstrip("/") + "/openai/v1"
    config = (
        f'model_provider = "{PROVIDER_ID}"\n\n'
        f"[model_providers.{PROVIDER_ID}]\n"
        'name = "Azure OpenAI"\n'
        f"base_url = {json.dumps(base_url)}\n"
        f"env_key = {json.dumps(agent['api_key_secret'])}\n"
        'wire_api = "responses"\n'
    )
    key = agent["api_key_secret"]
    return Launch([], env={key: secrets[key]}, files={spec.home / "config.toml": config})


class CodexReader(Reader):
    def __init__(self):
        self.started = set()
        self.message = None
        self.completed = False
        self.failed = None
        self.errors = []
        self.tokens = {}

    def events(self, raw):
        kind = raw.get("type")
        if kind == "turn.started":
            return [Event("turn")]
        if kind == "turn.completed":
            self.completed = True
            for key, value in (raw.get("usage") or {}).items():
                if isinstance(value, int) and not isinstance(value, bool):
                    self.tokens[key] = self.tokens.get(key, 0) + value
            return []
        if kind == "turn.failed":
            error = raw.get("error") if isinstance(raw.get("error"), dict) else {}
            self.failed = str(error.get("message") or "the turn failed")
            return [Event("notice", text=self.failed)]
        if kind == "error":
            self.errors.append(str(raw.get("message") or ""))
            return [Event("notice", text=self.errors[-1])]
        item = raw.get("item")
        if kind not in ("item.started", "item.completed") or not isinstance(item, dict):
            return []
        return self._item(item, done=kind == "item.completed")

    def _item(self, item, done):
        kind, item_id = item.get("type"), item.get("id")
        if kind == "agent_message" and done:
            self.message = str(item.get("text") or "")
            return [Event("text", text=self.message)]
        if kind == "error" and done:
            return [Event("notice", text=str(item.get("message") or ""))]
        call = self._call(item)
        if not call:
            return []
        found = []
        if item_id not in self.started:
            self.started.add(item_id)
            found.append(call)
        if done:
            found.append(self._result(item))
        return found

    def _call(self, item):
        kind, item_id = item.get("type"), item.get("id")
        if kind == "command_execution":
            command = str(item.get("command") or "")
            return Event(
                "tool_call", id=item_id, name="shell", input={"command": command}, command=command
            )
        if kind == "file_change":
            paths = [str(c.get("path")) for c in item.get("changes") or [] if isinstance(c, dict)]
            return Event(
                "tool_call", id=item_id, name="edit", input={"file_path": ", ".join(paths)}
            )
        if kind == "mcp_tool_call":
            name = f"{item.get('server', 'mcp')}.{item.get('tool', 'tool')}"
            return Event("tool_call", id=item_id, name=name, input=item.get("arguments") or {})
        if kind == "web_search":
            query = str(item.get("query") or "")
            return Event("tool_call", id=item_id, name="web_search", input={"description": query})
        return None

    def _result(self, item):
        kind = item.get("type")
        failed = item.get("status") in ("failed", "declined")
        if kind == "command_execution":
            text = str(item.get("aggregated_output") or "")
            failed = failed or item.get("exit_code") not in (0, None)
        elif kind == "file_change":
            changes = [c for c in item.get("changes") or [] if isinstance(c, dict)]
            text = "\n".join(f"{c.get('kind', 'update')} {c.get('path')}" for c in changes)
        else:
            result = item.get("result") or item.get("error") or ""
            text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        return Event("tool_result", id=item.get("id"), text=text, is_error=failed)

    def outcome(self, returncode, stderr):
        used = self.tokens
        outcome = Outcome(
            # Codex counts cached input in `input_tokens` and reasoning in `output_tokens`.
            tokens={
                "input": used.get("input_tokens", 0),
                "cached": used.get("cached_input_tokens", 0),
                "output": used.get("output_tokens", 0),
            }
            if used
            else None
        )
        if self.failed:
            outcome.error = f"stopped: {self.failed}"
        elif not self.completed:
            said = self.errors[-1] if self.errors else stderr.strip()[-1500:]
            outcome.error = f"exited with code {returncode} without a result. {said}".strip()
        if outcome.error:
            # Only what Codex itself said last, never the model's messages: a retry that met a
            # limit before the turn failed for another reason is not a limit.
            said = [self.failed] if self.failed else [*self.errors[-1:], stderr.strip()[-1500:]]
            outcome.limit = limit_reached(said)
        else:
            try:
                outcome.output = json.loads(self.message or "")
            except json.JSONDecodeError:
                outcome.output = None
        return outcome


class Codex(Runtime):
    name = "codex"
    label = "Codex"
    version = CODEX
    pin = "CODEX"
    release_url = f"https://registry.npmjs.org/{PACKAGE}/latest"
    program = "codex"
    efforts = ("none", "minimal", "low", "medium", "high", "xhigh")
    supports_budget = False
    reports_limits = True
    providers = {"azure-openai": azure_openai}

    def release_version(self, text):
        return str(json.loads(text).get("version", ""))

    def install(self):
        command = [
            "npm",
            "install",
            "--prefix",
            str(INSTALL_DIR),
            "--no-audit",
            "--no-fund",
            "--ignore-scripts",
            f"{PACKAGE}@{self.version}",
        ]
        return [command, ["bash", "-c", USER_NAMESPACES]], INSTALL_DIR / "node_modules" / ".bin"

    def launch(self, agent, spec, secrets, program=None):
        schema = spec.home / "schema.json"
        argv = [
            program or self.program,
            "exec",
            "--json",
            "--ephemeral",
            "--skip-git-repo-check",
            "--strict-config",
            "--ignore-rules",
            "--sandbox",
            SANDBOX[spec.access],
            "--model",
            spec.model,
            "--output-schema",
            str(schema),
            "-c",
            'web_search="disabled"',
            # Commands the agent runs never see the credential.
            "-c",
            f"shell_environment_policy.exclude={json.dumps(sorted(secrets))}",
        ]
        if spec.effort:
            argv += ["-c", f"model_reasoning_effort={json.dumps(spec.effort)}"]
        connection = self.connect(agent, spec, secrets)
        files = {schema: json.dumps(strict(spec.schema)), **connection.files}
        env = {**connection.env, "CODEX_HOME": str(spec.home)}
        return Launch([*argv, *connection.argv, "-"], env, files)

    def reader(self):
        return CodexReader()
