"""Claude Code: `claude -p` with stream-json output and a JSON schema for the result."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .. import CLAUDE_CODE
from .base import Event, Launch, Outcome, Reader, Runtime

READ_ONLY_TOOLS = "Read,Grep,Glob"
CREDENTIALS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
INSTALL_URL = "https://claude.ai/install.sh"


def anthropic(agent, spec, secrets):
    """Claude's own service. Claude Code reads the token or key from its own variable, so
    the agent's secret, whatever its name, is passed under that one."""
    env = {}
    for key, variable in zip(("oauth_token_secret", "api_key_secret"), CREDENTIALS, strict=True):
        if secrets.get(agent.get(key)):
            env[variable] = secrets[agent[key]]
    return Launch([], env=env)


# Claude Code says it stopped at a usage limit in plain text, for example `You've hit your
# session limit · resets 4:40am (UTC)`, and older versions as `Claude AI usage limit
# reached|1760000000`. Text that names a limit which resets: a credit balance that is too
# low never resets.
LIMIT = re.compile(
    r"usage limit|session limit|weekly limit|\d+-hour limit|limit reached"
    r"|(?:hit|reached) your [\w .-]{0,40}limit",
    re.I,
)
EPOCH = re.compile(r"limit reached\|(\d{9,11})\b", re.I)
# What Claude Code says when the service or the network failed for a while rather than the
# work: an overloaded or failing API, a rate limit that is not a usage limit, a lost
# connection. Another try later may pass.
TRANSIENT = re.compile(
    r"API Error: (?:5\d\d|429)\b|overloaded|internal server error|service unavailable"
    r"|bad gateway|gateway timeout|Connection error|ECONNRESET|ECONNREFUSED|ETIMEDOUT"
    r"|EAI_AGAIN|socket hang up|Request timed out|fetch failed",
    re.I,
)
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
RESET = re.compile(
    r"\breset(?:s)?\s+(?:at\s+|on\s+)?"
    r"(?:(?P<month>[A-Za-z]{3})[a-z]*\.?\s+(?P<day>\d{1,2}),?\s+(?:at\s+)?)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>[ap]\.?m\.?)?"
    r"(?:\s*\((?P<zone>[^)]{1,40})\))?",
    re.I,
)


def _zone(name):
    try:
        return ZoneInfo(name.strip()) if name else UTC
    except (ZoneInfoNotFoundError, ValueError):
        return UTC


def reset_time(text, now):
    """The UTC time at which the limit in `text` resets, or None when it gives none."""
    epoch = EPOCH.search(text)
    if epoch:
        return datetime.fromtimestamp(int(epoch.group(1)), UTC)
    match = RESET.search(text)
    if not match:
        return None
    hour, minute = int(match["hour"]), int(match["minute"] or 0)
    ampm = (match["ampm"] or "").lower().replace(".", "")
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59 or (not ampm and not match["minute"]):
        return None
    zone = _zone(match["zone"])
    local = now.astimezone(zone)
    if match["month"]:
        month = match["month"][:3].lower()
        if month not in MONTHS:
            return None
        try:
            when = local.replace(
                month=MONTHS.index(month) + 1,
                day=int(match["day"]),
                hour=hour,
                minute=minute,
                second=0,
                microsecond=0,
            )
        except ValueError:
            return None
        if when < local - timedelta(days=1):
            when = when.replace(year=when.year + 1)
    else:
        when = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if when <= local:
            when += timedelta(days=1)
    return when.astimezone(UTC)


def limit_reached(texts, now=None, resets_at=None):
    """When one of `texts` says the agent hit a usage limit: the line that says so and when
    the limit resets (UTC), or None for the reset when no text says. Otherwise None.

    `resets_at` is a reset time in Unix seconds that the CLI reported in its event stream;
    it wins over the text."""
    now = now or datetime.now(UTC)
    for text in texts:
        # The CLI's message is short and says it first; longer text is something else.
        text = str(text or "").strip()
        line = text.splitlines()[0][:300] if text else ""
        if not LIMIT.search(line) or len(text) > 1000:
            continue
        when = None
        if isinstance(resets_at, (int, float)) and resets_at > 0:
            when = datetime.fromtimestamp(resets_at, UTC)
        when = when or reset_time(text, now)
        if when is not None and when <= now - timedelta(hours=1):
            when = None
        return {"message": line, "reset": when}
    return None


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


# A model's tokens in a result event: (`modelUsage` key, `usage` key).
TOKEN_KEYS = {
    "input": ("inputTokens", "input_tokens"),
    "written": ("cacheCreationInputTokens", "cache_creation_input_tokens"),
    "read": ("cacheReadInputTokens", "cache_read_input_tokens"),
    "output": ("outputTokens", "output_tokens"),
}


def tokens(result):
    """The tokens of a result event over every model the session used: `modelUsage` has
    one entry per model, such as a subagent's, and `usage` only the main model's."""
    models = [m for m in (result.get("modelUsage") or {}).values() if isinstance(m, dict)]
    column = 0
    if not models:
        if not isinstance(result.get("usage"), dict):
            return None
        models, column = [result["usage"]], 1
    total = dict.fromkeys(TOKEN_KEYS, 0)
    for model in models:
        for name, keys in TOKEN_KEYS.items():
            value = model.get(keys[column])
            if isinstance(value, int) and not isinstance(value, bool):
                total[name] += value
    # Claude counts the input written to and read from the cache apart from the rest.
    return {
        "input": total["input"] + total["written"] + total["read"],
        "cached": total["read"],
        "output": total["output"],
    }


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


# The tool through which Claude Code takes the result that `--json-schema` asks for.
RESULT_TOOL = "StructuredOutput"


class ClaudeReader(Reader):
    def __init__(self):
        self.result = None
        self.last_text = ""
        self.resets_at = None
        self._messages = set()
        # The input of each `StructuredOutput` call, by id, until its tool result arrives.
        self._submitting = {}
        # The last result `StructuredOutput` accepted. The `result` event usually carries it
        # as `structured_output`, but not always when the model kept talking afterwards.
        self.submitted = None
        # Commands started with `run_in_background`: a headless run cannot wait for them.
        self.background = 0
        # Whether the model wrote anything, rather than Claude Code alone.
        self.answered = False

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
            self.answered = self.answered or message.get("model") != "<synthetic>"
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
                    if name == RESULT_TOOL:
                        self._submitting[block.get("id")] = data
                    if isinstance(data, dict) and data.get("run_in_background"):
                        self.background += 1
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
                    data = self._submitting.pop(block.get("tool_use_id"), None)
                    if data is not None and not block.get("is_error"):
                        self.submitted = data
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
        outcome = Outcome(tokens=tokens(event))
        if not self.result:
            outcome.error = (
                f"exited with code {returncode} without a result. {stderr.strip()[-1500:]}"
            ).strip()
        elif event.get("is_error") or event.get("subtype") != "success":
            outcome.error = f"stopped ({event.get('subtype')}). {event.get('result') or ''}"
        else:
            outcome.output = event.get("structured_output")
            if outcome.output is None:
                outcome.output = self.submitted
            if outcome.output is None and self.background:
                outcome.error = (
                    "ended its turn after starting commands in the background, which a "
                    "NexKit run cannot wait for, and returned no structured result."
                )
        # Only what Claude Code says about a failed run, never the model's own text.
        said = event.get("result") if event.get("is_error") else None
        outcome.limit = limit_reached((said, stderr, self.last_text), resets_at=self.resets_at)
        if outcome.error and not outcome.limit:
            texts = " ".join(str(text) for text in (said, stderr, self.last_text) if text)
            # A CLI that exited before the model answered never got to the work.
            outcome.transient = bool(TRANSIENT.search(texts)) or (
                not self.result and not self.answered
            )
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
    reports_limits = True
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
        # Nothing wakes a headless session once its turn ends, so a command left running in
        # the background would end the run without a result: run everything in the foreground.
        env = {**connection.env, "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"}
        return Launch(cmd + connection.argv, env, connection.files)

    def reader(self):
        return ClaudeReader()
