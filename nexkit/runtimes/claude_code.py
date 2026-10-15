"""Claude Code: `claude -p` with stream-json output and a JSON schema for the result."""

from __future__ import annotations

import json
from pathlib import Path

from .. import CLAUDE_CODE
from ..usage import limit_reached
from .base import Event, Launch, Outcome, Reader, Runtime

READ_ONLY_TOOLS = "Read,Grep,Glob"
CREDENTIALS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
INSTALL_URL = "https://claude.ai/install.sh"


def anthropic(agent, spec, secrets):
    """Claude's own service, with a subscription token or an API key."""
    return Launch([], env=dict(secrets))


def result_text(content):
    """The text of a tool result's content."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif isinstance(item, dict):
                parts.append(f"[{item.get('type', 'content')}]")
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return "" if content is None else json.dumps(content, ensure_ascii=False)


def _text_of(event):
    """The text of a message that Claude Code wrote itself, such as an API or usage limit
    error, or "". The model's own words are left out: they may mention limits."""
    message = event.get("message") if event.get("type") == "assistant" else None
    if not isinstance(message, dict) or message.get("model") != "<synthetic>":
        return ""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    texts = [b.get("text") for b in content if isinstance(b, dict) and b.get("type") == "text"]
    return "\n".join(str(t) for t in texts if t)


def _rejected_until(event):
    """The reset time (Unix seconds) of a rate limit event that rejected the request."""
    info = event.get("rate_limit_info") if event.get("type") == "rate_limit_event" else None
    if not isinstance(info, dict) or info.get("status") != "rejected":
        return None
    value = info.get("resetsAt", info.get("resets_at"))
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


class ClaudeReader(Reader):
    def __init__(self):
        self.result = None
        self.last_text = ""
        self.resets_at = None
        self._messages = set()

    def events(self, raw):
        if raw.get("type") == "result":
            self.result = raw
        self.last_text = _text_of(raw) or self.last_text
        self.resets_at = _rejected_until(raw) or self.resets_at
        message = raw.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return []
        found = []
        if raw.get("type") == "assistant":
            message_id = message.get("id")
            if message_id is None or message_id not in self._messages:
                self._messages.add(message_id)
                found.append(Event("turn"))
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and str(block.get("text", "")).strip():
                    found.append(Event("text", text=str(block["text"])))
                elif block.get("type") == "tool_use":
                    data = block.get("input") or {}
                    name = block.get("name") or "tool"
                    command = data.get("command") if isinstance(data, dict) else None
                    found.append(
                        Event(
                            "tool_call",
                            id=block.get("id"),
                            name=name,
                            input=data,
                            command=str(command) if name == "Bash" and command else None,
                        )
                    )
        elif raw.get("type") == "user":
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    found.append(
                        Event(
                            "tool_result",
                            id=block.get("tool_use_id"),
                            text=result_text(block.get("content")),
                            is_error=bool(block.get("is_error")),
                        )
                    )
        return found

    def outcome(self, returncode, stderr):
        event = self.result or {}
        outcome = Outcome(cost_usd=event.get("total_cost_usd"), turns=event.get("num_turns"))
        if not self.result:
            outcome.error = (
                f"exited with code {returncode} without a result. {stderr.strip()[-1500:]}"
            ).strip()
        elif event.get("is_error") or event.get("subtype") != "success":
            outcome.error = f"stopped ({event.get('subtype')}). {event.get('result') or ''}"
        else:
            outcome.output = event.get("structured_output")
        # Only what Claude Code says about a failed run, never the model's own text.
        said = event.get("result") if event.get("is_error") else None
        outcome.limit = limit_reached((said, stderr, self.last_text), resets_at=self.resets_at)
        return outcome


class ClaudeCode(Runtime):
    name = "claude-code"
    label = "Claude"
    version = CLAUDE_CODE
    pin = "CLAUDE_CODE"
    release_url = "https://downloads.claude.ai/claude-code-releases/stable"
    program = "claude"
    efforts = ("low", "medium", "high", "xhigh", "max")
    supports_budget = True
    providers = {"anthropic": anthropic}

    def install(self):
        script = f'curl -fsSL {INSTALL_URL} | bash -s -- "$0"'
        return [["bash", "-o", "pipefail", "-c", script, self.version]], Path.home() / ".local/bin"

    def launch(self, agent, spec, secrets, program=None):
        cmd = [
            program or self.program,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--no-session-persistence",
            "--model",
            spec.model,
            "--json-schema",
            json.dumps(spec.schema),
        ]
        if spec.effort:
            cmd += ["--effort", spec.effort]
        if spec.budget_usd:
            cmd += ["--max-budget-usd", str(spec.budget_usd)]
        if spec.access == "none":
            cmd += ["--tools", ""]
        elif spec.access == "read":
            cmd += ["--tools", READ_ONLY_TOOLS]
        else:
            # The job runs on a disposable runner without any GitHub write token.
            cmd += ["--permission-mode", "bypassPermissions"]
        connection = self.connect(agent, spec, secrets)
        return Launch(cmd + connection.argv, connection.env, connection.files)

    def reader(self):
        return ClaudeReader()
