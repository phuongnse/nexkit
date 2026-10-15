"""Codex CLI: `codex exec` with JSONL events and an output schema for the result.

NexKit gives each run an empty CODEX_HOME holding only the provider's `config.toml`, so no
user configuration, login or session history applies. Project `.rules` files are ignored,
so a repository cannot widen what a read-only stage may run.
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import CODEX
from ..schema import strict
from .base import Event, Launch, Outcome, Reader, Runtime

PACKAGE = "@openai/codex"
INSTALL_DIR = Path.home() / ".nexkit" / "codex"
SANDBOX = {"none": "read-only", "read": "read-only", "write": "danger-full-access"}
# Codex's provider id in the generated config.toml.
PROVIDER_ID = "nexkit"


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
        outcome = Outcome(tokens=self.tokens or None)
        if self.failed:
            outcome.error = f"stopped: {self.failed}"
        elif not self.completed:
            said = self.errors[-1] if self.errors else stderr.strip()[-1500:]
            outcome.error = f"exited with code {returncode} without a result. {said}".strip()
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
        return [command], INSTALL_DIR / "node_modules" / ".bin"

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
