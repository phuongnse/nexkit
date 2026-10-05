"""Pipeline state lives in GitHub comments written by the Actions bot."""

from __future__ import annotations

import base64
import json
import re

BOT_LOGIN = "github-actions[bot]"
PLAN_MARKER = "<!-- nexkit:plan -->"
STATE_PREFIX = "<!-- nexkit:state "
STATE_PATTERN = re.compile(r"<!-- nexkit:state ([A-Za-z0-9+/=]+) -->")


def by_bot(comment):
    return (comment.get("user") or {}).get("login") == BOT_LOGIN


def latest_plan(comments):
    """The most recent plan comment posted by NexKit, or None."""
    plans = [c for c in comments if by_bot(c) and c.get("body", "").startswith(PLAN_MARKER)]
    return plans[-1] if plans else None


def plan_text(comment):
    return comment["body"].removeprefix(PLAN_MARKER).strip()


def empty_state(issue):
    return {"version": 1, "issue": issue, "auto_fixes": 0, "rounds": [], "feedback": None}


def read_state(comments):
    """Return (comment_id, state) for the PR's NexKit state comment, or (None, None)."""
    for comment in reversed(comments):
        if not by_bot(comment):
            continue
        match = STATE_PATTERN.search(comment.get("body", ""))
        if match:
            return comment["id"], json.loads(base64.b64decode(match.group(1)))
    return None, None


def _cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_state(state):
    lines = [
        "### NexKit",
        "",
        f"Working on #{state['issue']}. Automatic fix rounds used: {state['auto_fixes']}.",
        "",
        "| Round | Trigger | Commit | Checks | AI review | Cost |",
        "|---|---|---|---|---|---|",
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
    encoded = base64.b64encode(json.dumps(state, sort_keys=True).encode()).decode()
    return "\n".join(lines) + f"\n\n{STATE_PREFIX}{encoded} -->"
