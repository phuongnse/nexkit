"""Show a Claude Code event stream: the Actions log, transcripts and the run summary.

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


def command_data(text):
    """Escape a workflow command's message so it stays on one line."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def fence(text, char="`"):
    """A code fence longer than any run of `char` in the text."""
    runs = re.findall(f"{re.escape(char)}+", text)
    return char * max([3, *(len(run) + 1 for run in runs)])


def code(text):
    """Inline code that survives backticks in the text."""
    ticks = "`" * (max((len(run) for run in re.findall("`+", text)), default=0) + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{ticks}{pad}{text}{pad}{ticks}"


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


def result_text(content):
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


def result_label(call):
    return {True: "error", False: "ok"}.get(call["is_error"], "no result (the run ended first)")


class RunLog:
    """Consumes Claude Code's stream-json lines for one run.

    It prints the Actions log (one line per message and tool call with `tool_output`
    `none`, a collapsible group per tool call with `truncated`), and when `transcript_dir`
    is set writes a redacted `transcript.jsonl` and a readable `transcript.md` there.
    Claude's thinking is never printed and is left out of `transcript.md`.
    """

    def __init__(
        self,
        redact,
        *,
        title="Claude Code",
        tool_output="truncated",
        transcript_dir=None,
        timer=time.monotonic,
    ):
        self.redact = redact
        self.tool_output = tool_output
        self.timer = timer
        self.started = timer()
        self.turn = 0
        self.now = 0.0  # seconds since the start, at the current event
        self.calls = []
        self._pending = {}
        self._messages = set()
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
        """Handle one line of output. Returns the parsed event, or None."""
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            event = None
        if self._jsonl:
            if event is None:
                self._jsonl.write(self.redact(raw.rstrip("\n")) + "\n")
            else:
                self._jsonl.write(json.dumps(self.redact.data(event), ensure_ascii=False) + "\n")
            self._jsonl.flush()
        if isinstance(event, dict):
            self.event(event)
            return event
        return None

    def event(self, event):
        self.now = self.timer() - self.started
        kind = event.get("type")
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return
        if kind == "assistant":
            message_id = message.get("id")
            if message_id is None or message_id not in self._messages:
                self._messages.add(message_id)
                self.turn += 1
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and str(block.get("text", "")).strip():
                    self._text(str(block["text"]))
                elif block.get("type") == "tool_use":
                    self._tool_use(block)
        elif kind == "user":
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    call = self._pending.pop(block.get("tool_use_id"), None)
                    if call:
                        output = result_text(block.get("content"))
                        self._finish(call, output, bool(block.get("is_error")))

    def _text(self, text):
        text = self.redact(text.strip())
        self._print("· " + one_line(text)[:240])
        self._md(f"\n## [{clock(self.now)} #{self.turn}] Claude\n\n{text}\n")

    def _tool_use(self, block):
        # Redact before shortening, so a cut cannot split a secret.
        data = self.redact.data(block.get("input") or {})
        call = {
            "name": one_line(block.get("name") or "tool"),
            "detail": one_line(tool_detail(data)),
            "input": input_text(data),
            "at": self.now,
            "turn": self.turn,
            "is_error": None,
            "output": "",
        }
        self.calls.append(call)
        self._pending[block.get("id")] = call
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
            token = secrets.token_hex(16)
            while token in body:
                token = secrets.token_hex(16)
            # Workflow commands in the output (::set-output, ::add-mask::, ...) are ignored.
            self._print(f"::stop-commands::{token}")
            self._print(body)
            self._print(f"::{token}::")
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
    turns, cost = result.get("turns"), result.get("cost")
    columns = {
        "Result": status,
        "Turns": turns if turns is not None else "-",
        "Duration": duration(result.get("seconds")),
        "Cost": f"${cost:.2f}" if cost is not None else "-",
    }
    output = result.get("output") or {}
    if result.get("stage") == "review":
        verdict = output.get("verdict")
        columns["Verdict"] = verdict if verdict in ("approve", "request_changes") else "-"
    lines = [
        f"### NexKit {result.get('stage', 'agent')}: {status}",
        "",
        "| " + " | ".join(columns) + " |",
        "|" + "---|" * len(columns),
        "| " + " | ".join(map(str, columns.values())) + " |",
    ]
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
    commands = [call for call in calls if call["name"] == "Bash"]
    if commands:
        marks = {True: "✗", False: "✓"}
        rows = [f"{marks.get(call['is_error'], '…')} {call['detail'][:200]}" for call in commands]
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


def _block(text):
    return f"{fence(text)}\n{text}\n{fence(text)}"


def write_summary(text, redact):
    """Append to the job summary on GitHub Actions; print nothing elsewhere."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(redact(text) + "\n")
