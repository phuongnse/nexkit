"""Show an agent's run: the Actions log, transcripts and the run summary.

Everything shown comes from the agent and the repository, so it is untrusted: it is
redacted first, and tool output is printed with GitHub workflow commands stopped.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from pathlib import Path

from .runtimes.base import TOKENS

HEAD_LINES = 20
MAX_CHARS = 4000


def truncate(text, lines=HEAD_LINES, chars=MAX_CHARS):
    """Keep the first and last `lines` lines, then at most `chars` characters."""
    rows = text.splitlines()
    if len(rows) > 2 * lines:
        omitted = len(rows) - 2 * lines
        rows = [*rows[:lines], f"[… {omitted} lines omitted …]", *rows[-lines:]]
    text = "\n".join(rows)
    if len(text) > chars:
        half = chars // 2
        omitted = len(text) - 2 * half
        text = f"{text[:half]}\n[… {omitted} characters omitted …]\n{text[-half:]}"
    return text


def one_line(text):
    return " ".join(str(text).split())


def clock(seconds):
    seconds = int(seconds)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def duration(seconds):
    seconds = int(seconds or 0)
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


# What a table cell shows when there is nothing to count or say.
NO_DATA = "N/A"


def add_tokens(*counts):
    """The sum of runs' tokens, or None when a run has no count: a total that left out a
    run would read as the whole."""
    if not counts or not all(counts):
        return None
    return {key: sum(count.get(key) or 0 for count in counts) for key in TOKENS}


def tokens_text(tokens):
    """`1.24M in (1.1M cached), 15.2k out`, or N/A when there is no count."""
    if not tokens:
        return NO_DATA
    cached = tokens.get("cached") or 0
    cached = f" ({_count(cached)} cached)" if cached else ""
    return f"{_count(tokens.get('input') or 0)} in{cached}, {_count(tokens.get('output') or 0)} out"


def _count(number):
    """A count in three significant digits: 950, 15.2k, 1.24M."""
    if number < 1000:
        return str(number)
    for unit in ("k", "M", "B"):
        number /= 1000
        digits = 2 if number < 9.995 else 1 if number < 99.95 else 0
        if round(number, digits) < 1000 or unit == "B":
            text = f"{number:.{digits}f}"
            return (text.rstrip("0").rstrip(".") if digits else text) + unit


def command_data(text):
    """Escape a workflow command's message so it stays on one line."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def stopped(text):
    """`text` between workflow commands that stop and resume them, so that commands in it
    (::set-output, ::add-mask::, ...) are ignored when it is printed."""
    token = secrets.token_hex(16)
    while token in text:
        token = secrets.token_hex(16)
    return f"::stop-commands::{token}\n{text}\n::{token}::"


def fence(text, char="`"):
    """A code fence longer than any run of `char` in the text."""
    runs = re.findall(f"{re.escape(char)}+", text)
    return char * max([3, *(len(run) + 1 for run in runs)])


def code(text):
    """Inline code that survives backticks in the text."""
    ticks = "`" * (max((len(run) for run in re.findall("`+", text)), default=0) + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{ticks}{pad}{text}{pad}{ticks}"


def agent_text(name, model=None):
    """An agent as every comment and summary names it: `claude` (`claude-opus-5-5`)."""
    return f"{code(name)} ({code(one_line(model))})" if model else code(name)


def stage_agent(stage, name, model=None):
    """A stage and the agent that ran it, or will: plan `claude` (`claude-opus-5-5`)."""
    return f"{stage} {agent_text(name, model)}"


def tool_detail(data):
    """The short description of a tool call shown in its title."""
    data = data if isinstance(data, dict) else {}
    return str(
        data.get("command")
        or data.get("file_path")
        or data.get("pattern")
        or data.get("description")
        or ""
    )


def input_text(data):
    """A tool call's input, one `key: value` per line. Long values are shortened."""
    if not isinstance(data, dict):
        return truncate(json.dumps(data, ensure_ascii=False))
    lines = []
    for key, value in data.items():
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        text = truncate(text)
        if "\n" in text:
            lines += [f"{key}:", *("  " + row for row in text.splitlines())]
        else:
            lines.append(f"{key}: {text}")
    return "\n".join(lines)


def result_label(call):
    return {True: "error", False: "ok"}.get(call["is_error"], "no result (the run ended first)")


class RunLog:
    """Consumes the output lines of one agent run.

    The runtime's `reader` turns each line into events. RunLog prints the Actions log (one
    line per message and tool call with `tool_output` `none`, a collapsible group per tool
    call with `truncated`), and when `transcript_dir` is set writes a redacted
    `transcript.jsonl` of the raw lines and a readable `transcript.md` there. The model's
    thinking is never printed and is left out of `transcript.md`.
    """

    def __init__(
        self,
        redact,
        *,
        reader,
        title="NexKit agent",
        speaker="Agent",
        tool_output="truncated",
        transcript_dir=None,
        timer=time.monotonic,
    ):
        self.redact = redact
        self.reader = reader
        self.speaker = speaker
        self.tool_output = tool_output
        self.timer = timer
        self.started = timer()
        self.turn = 0
        self.now = 0.0  # seconds since the start, at the current event
        self.calls = []
        self._pending = {}
        self._jsonl = self._markdown = None
        if transcript_dir is not None:
            directory = Path(transcript_dir)
            self._jsonl = (directory / "transcript.jsonl").open("w", encoding="utf-8")
            self._markdown = (directory / "transcript.md").open("w", encoding="utf-8")
            self._markdown.write(f"# {title} transcript\n")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _print(self, text):
        print(text, flush=True)

    def _md(self, text):
        if self._markdown:
            self._markdown.write(text)
            self._markdown.flush()

    def line(self, raw):
        """Handle one line of output. Returns the parsed line, or None."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = None
        if self._jsonl:
            if data is None:
                self._jsonl.write(self.redact(raw.rstrip("\n")) + "\n")
            else:
                self._jsonl.write(json.dumps(self.redact.data(data), ensure_ascii=False) + "\n")
            self._jsonl.flush()
        if not isinstance(data, dict):
            return None
        self.now = self.timer() - self.started
        for event in self.reader.events(data):
            self.show(event)
        return data

    def show(self, event):
        if event.kind == "turn":
            self.turn += 1
        elif event.kind == "text" and event.text.strip():
            self._text(event.text)
        elif event.kind == "notice" and event.text.strip():
            self._notice(event.text)
        elif event.kind == "tool_call":
            self._tool_use(event)
        elif event.kind == "tool_result":
            call = self._pending.pop(event.id, None)
            if call:
                self._finish(call, event.text, event.is_error)

    def _text(self, text):
        text = self.redact(text.strip())
        self._print("· " + one_line(text)[:240])
        self._md(f"\n## [{clock(self.now)} #{self.turn}] {self.speaker}\n\n{text}\n")

    def _notice(self, text):
        text = self.redact(text.strip())
        self._print("! " + one_line(text)[:240])
        self._md(f"\n> [{clock(self.now)}] {one_line(text)}\n")

    def _tool_use(self, event):
        # Redact before shortening, so a cut cannot split a secret.
        data = self.redact.data(event.input or {})
        call = {
            "name": one_line(event.name or "tool"),
            "detail": one_line(tool_detail(data)),
            "command": self.redact(event.command) if event.command else None,
            "input": input_text(data),
            "at": self.now,
            "turn": self.turn,
            "is_error": None,
            "output": "",
        }
        self.calls.append(call)
        self._pending[event.id] = call
        if self.tool_output == "none":
            self._print(f"▸ {call['name']} {call['detail'][:200]}")

    def _finish(self, call, output, is_error):
        call["is_error"] = is_error
        call["output"] = truncate(self.redact(output))
        title = f"[{clock(call['at'])} #{call['turn']}] ▸ {call['name']} {call['detail'][:120]}"
        title = title.rstrip()
        body = (
            f"Input:\n{call['input']}\n\nResult: {result_label(call)}\n"
            f"{call['output'] or '(no output)'}"
        )
        if self.tool_output == "truncated":
            self._print(f"::group::{command_data(title)}")
            self._print(stopped(body))
            self._print("::endgroup::")
            if call["is_error"]:
                first = next((row for row in call["output"].splitlines() if row.strip()), "")
                warning = f"{call['name']} failed: {one_line(first)[:300]}".strip()
                self._print(f"::warning::{command_data(warning)}")
        inputs = call["input"] or "(no input)"
        output = call["output"] or "(no output)"
        self._md(
            f"\n### {title}\n\nInput:\n\n{fence(inputs)}\n{inputs}\n{fence(inputs)}\n\n"
            f"Result: {result_label(call)}\n\n{fence(output)}\n{output}\n{fence(output)}\n"
        )

    def close(self):
        for call in list(self._pending.values()):
            self._finish(call, "", None)
        self._pending.clear()
        for handle in (self._jsonl, self._markdown):
            if handle:
                handle.close()
        self._jsonl = self._markdown = None


def summary(result, calls):
    """The Markdown run summary for an agent or review job."""
    status = result.get("status") or "error"
    columns = {"Result": status, "Duration": duration(result.get("seconds"))}
    if result.get("tokens"):
        columns["Tokens"] = tokens_text(result["tokens"])
    output = result.get("output") or {}
    if result.get("stage") == "review":
        verdict = output.get("verdict")
        columns["Verdict"] = verdict if verdict in ("approve", "request_changes") else NO_DATA
    lines = [
        f"### NexKit {result.get('stage', 'agent')}: {status}",
        "",
        "| " + " | ".join(columns) + " |",
        "|" + "---|" * len(columns),
        "| " + " | ".join(map(str, columns.values())) + " |",
    ]
    if result.get("profile"):
        lines += ["", f"**Profile:** {code(one_line(result['profile']))}"]
    lines += _agents(result)
    if result.get("error"):
        lines += ["", "**Why it stopped**", "", _block(result["error"])]
    if result.get("start_base"):
        conflicts = result.get("conflicts") or []
        lines += [
            "",
            f"**Merged the base branch** (`{str(result['start_base'])[:7]}`); "
            f"files with conflicts ({len(conflicts)}):",
            "",
            _block("\n".join(conflicts)),
        ]
        # The files changed below include what the merge brought from the base branch.
    if result.get("changed_files") is not None:
        files = result["changed_files"]
        lines += ["", f"**Files changed** ({len(files)})"]
        if files:
            lines += ["", _block("\n".join(files))]
    commands = [call for call in calls if call.get("command")]
    if commands:
        marks = {True: "✗", False: "✓"}
        rows = [
            f"{marks.get(call['is_error'], '…')} {one_line(call['command'])[:200]}"
            for call in commands
        ]
        lines += ["", f"**Commands run** ({len(commands)}; ✗ failed)", "", _block("\n".join(rows))]
    findings = output.get("findings") if result.get("stage") == "review" else None
    if isinstance(findings, list):
        lines += ["", f"**Findings** ({len(findings)})"]
        if findings:
            lines.append("")
        for item in findings:
            if not isinstance(item, dict):
                continue
            where = f"{item.get('file', '')}:{item.get('line', '')}"
            lines.append(
                f"- **{one_line(item.get('severity', ''))}** {code(one_line(where))}: "
                f"{one_line(item.get('body', ''))[:500]}"
            )
    return "\n".join(lines) + "\n"


# How the run summary names an agent's result.
RESULTS = {"paused": "usage limit", "skipped": "skipped"}


def _agents(result):
    """The agent that ran the stage, and when a fallback ran, every agent's run."""
    attempts = result.get("attempts") or []
    ran = next((a for a in attempts if a["agent"] == result.get("agent")), None)
    if not ran:
        return []
    stage = result.get("stage", "agent")
    lines = [
        "",
        f"**Agent:** {stage_agent(stage, ran['agent'], ran['model'])}, "
        f"{ran['runtime']} on {ran['provider']}",
    ]
    if len(attempts) < 2:
        return lines
    tokens = any(a.get("tokens") for a in attempts)
    header = ["Agent", "Model", "Result", "Duration", *(["Tokens"] if tokens else [])]
    lines += [
        "",
        "**Agents that ran in turn** (the next one starts when one stops at its usage limit)",
        "",
        "| " + " | ".join(header) + " |",
        "|" + "---|" * len(header),
    ]
    for attempt in attempts:
        outcome = RESULTS.get(attempt["status"], attempt["status"])
        if attempt["status"] == "skipped" and attempt.get("error"):
            outcome += f": {one_line(attempt['error'])[:200]}"
        cells = [
            code(attempt["agent"]),
            code(one_line(attempt["model"])),
            outcome,
            duration(attempt.get("seconds")) if attempt.get("seconds") is not None else NO_DATA,
        ]
        if tokens:
            cells.append(tokens_text(attempt.get("tokens")))
        lines.append("| " + " | ".join(cell.replace("|", "\\|") for cell in cells) + " |")
    return lines


def _block(text):
    return f"{fence(text)}\n{text}\n{fence(text)}"


def write_summary(text, redact):
    """Append to the job summary on GitHub Actions; print nothing elsewhere."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(redact(text) + "\n")
