"""Pipeline state lives in GitHub comments written by the Actions bot.

Two comments carry state, each edited in place: a status comment on the issue for its
`plan` and `go` runs, and a state comment on the pull request for its rounds.
"""

from __future__ import annotations

import base64
import json
import re

BOT_LOGIN = "github-actions[bot]"
PLAN_MARKER = "<!-- nexkit:plan -->"
STATE_PREFIX = "<!-- nexkit:state "
STATE_PATTERN = re.compile(r"<!-- nexkit:state ([A-Za-z0-9+/=]+) -->")
STATUS_PREFIX = "<!-- nexkit:status "
STATUS_PATTERN = re.compile(r"<!-- nexkit:status ([A-Za-z0-9+/=]+) -->")
MAX_RUNS = 10

RUNNING, SUCCESS, FAILURE = "running", "success", "failure"
RUN_ICONS = {RUNNING: "⏳", SUCCESS: "✅", FAILURE: "❌"}


def by_bot(comment):
    return (comment.get("user") or {}).get("login") == BOT_LOGIN


def clip(text, limit=4000):
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "\n[truncated]"


def latest_plan(comments):
    """The most recent plan comment posted by NexKit, or None."""
    plans = [c for c in comments if by_bot(c) and c.get("body", "").startswith(PLAN_MARKER)]
    return plans[-1] if plans else None


def plan_text(comment):
    return comment["body"].removeprefix(PLAN_MARKER).strip()


def _find(comments, pattern):
    for comment in reversed(comments):
        if not by_bot(comment):
            continue
        match = pattern.search(comment.get("body", ""))
        if match:
            return comment["id"], json.loads(base64.b64decode(match.group(1)))
    return None, None


def _encode(prefix, data):
    return f"{prefix}{base64.b64encode(json.dumps(data, sort_keys=True).encode()).decode()} -->"


def find_run(items, url):
    """The entry for the workflow run at `url`, or None."""
    return next((item for item in items if url and item.get("url") == url), None)


def run_link(item):
    """`⏳ [running](url)` and the like; `-` for rounds recorded before runs were linked."""
    status = item.get("status")
    if status not in RUN_ICONS:
        return "-"
    label = "running" if status == RUNNING else "run log"
    url = item.get("url")
    return f"{RUN_ICONS[status]} " + (f"[{label}]({url})" if url else label)


# -- pull request state -------------------------------------------------------------


def empty_state(issue):
    return {"version": 1, "issue": issue, "auto_fixes": 0, "rounds": [], "feedback": None}


def read_state(comments):
    """Return (comment_id, state) for the PR's NexKit state comment, or (None, None)."""
    return _find(comments, STATE_PATTERN)


def _cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_state(state):
    lines = [
        "### NexKit",
        "",
        f"Working on #{state['issue']}. Automatic fix rounds used: {state['auto_fixes']}.",
        "",
        "| Round | Trigger | Run | Commit | Checks | AI review | Cost |",
        "|---|---|---|---|---|---|---|",
    ]
    for item in state["rounds"]:
        cost = item.get("cost")
        lines.append(
            "| "
            + " | ".join(
                _cell(v)
                for v in (
                    item["round"],
                    item["trigger"],
                    run_link(item),
                    (item.get("head") or "-")[:7],
                    item.get("checks", "-"),
                    item.get("verdict", "-"),
                    f"${cost:.2f}" if isinstance(cost, (int, float)) else "-",
                )
            )
            + " |"
        )
    lines += [
        "",
        "Comment `/nexkit fix <instructions>` to request another round, or `/nexkit review` "
        "to re-check the current commit.",
    ]
    return "\n".join(lines) + "\n\n" + _encode(STATE_PREFIX, state)


# -- issue status -------------------------------------------------------------------


def empty_status():
    return {"version": 1, "runs": []}


def read_status(comments):
    """Return (comment_id, status) for the issue's NexKit status comment, or (None, None)."""
    return _find(comments, STATUS_PATTERN)


def render_status(status):
    lines = ["### NexKit", ""]
    for item in reversed(status["runs"]):
        line = f"- {run_link(item)} `/nexkit {item['command']}`"
        text = (item.get("text") or "").strip().replace("\n", "\n  ")
        lines.append(f"{line}: {text}" if text else line)
    return "\n".join(lines) + "\n\n" + _encode(STATUS_PREFIX, status)
