"""Pipeline state lives in GitHub comments written by the Actions bot.

Every run has its own run comment, posted where the run was started: on the issue for
`plan` and `go`, on the pull request for a round. A hidden marker in it holds the run URL,
so `report` edits the comment its run started. The state that later rounds read lives in
one state comment on the pull request, edited in place.
"""

from __future__ import annotations

import base64
import json
import re

BOT_LOGIN = "github-actions[bot]"
PLAN_MARKER = "<!-- nexkit:plan -->"
STATE_PREFIX = "<!-- nexkit:state "
STATE_PATTERN = re.compile(r"<!-- nexkit:state ([A-Za-z0-9+/=]+) -->")
RUN_PREFIX = "<!-- nexkit:run "
RUN_PATTERN = re.compile(r"<!-- nexkit:run ([A-Za-z0-9+/=]+) -->")
# A plan's profile: a hidden record on the line after the plan marker, where agent text
# cannot be, and lines for people that the agents do not receive.
PROFILE_PREFIX = "<!-- nexkit:profile "
PROFILE_RECORD = re.compile(r"\n<!-- nexkit:profile ([A-Za-z0-9+/=]+) -->")
PROFILE_TEXT = ("<!-- nexkit:profile-text -->", "<!-- /nexkit:profile-text -->")
PROFILE_TEXT_PATTERN = re.compile(
    r"<!-- nexkit:profile-text -->.*?<!-- /nexkit:profile-text -->\n*", re.S
)

RUNNING, SUCCESS, FAILURE, PAUSED = "running", "success", "failure", "paused"
RUN_ICONS = {RUNNING: "⏳", SUCCESS: "✅", FAILURE: "❌", PAUSED: "⏸️"}
RUN_LABELS = {RUNNING: "running", PAUSED: "paused"}


def by_bot(comment):
    return (comment.get("user") or {}).get("login") == BOT_LOGIN


def clip(text, limit=4000):
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "\n[truncated]"


def latest_plan(comments):
    """The most recent plan comment posted by NexKit, or None."""
    plans = [c for c in comments if by_bot(c) and c.get("body", "").startswith(PLAN_MARKER)]
    return plans[-1] if plans else None


def _profile_record(comment):
    """The match of the hidden profile record on the line after the plan marker, or None."""
    body = comment["body"]
    return PROFILE_RECORD.match(body, len(PLAN_MARKER)) if body.startswith(PLAN_MARKER) else None


def plan_text(comment):
    """The plan for the agents: without the profile, which is for people."""
    record = _profile_record(comment)
    body = comment["body"][record.end() if record else len(PLAN_MARKER) :]
    return PROFILE_TEXT_PATTERN.sub("", body).strip()


def profile_record(record):
    """The hidden line after a plan's marker. `record` holds `profile` and `chosen_by`."""
    return _encode(PROFILE_PREFIX, record)


def profile_text(text):
    """The profile lines of a plan comment, marked so the agents do not receive them."""
    return f"{PROFILE_TEXT[0]}\n\n{text.strip()}\n\n{PROFILE_TEXT[1]}"


def plan_profile(comment):
    """The profile record of a plan comment, or None for a plan without profiles."""
    match = _profile_record(comment)
    if not match:
        return None
    try:
        record = json.loads(base64.b64decode(match.group(1)))
    except ValueError:
        return None
    return record if isinstance(record, dict) and isinstance(record.get("profile"), str) else None


def _marked(comments, pattern):
    """(comment_id, data) for each bot comment with the marker, newest first."""
    for comment in reversed(comments):
        if not by_bot(comment):
            continue
        match = pattern.search(comment.get("body", ""))
        if match:
            yield comment["id"], json.loads(base64.b64decode(match.group(1)))


def _find(comments, pattern):
    return next(_marked(comments, pattern), (None, None))


def _encode(prefix, data):
    return f"{prefix}{base64.b64encode(json.dumps(data, sort_keys=True).encode()).decode()} -->"


def run_link(run):
    """`⏳ [running](url)`, `✅ [run log](url)`, `❌ [run log](url)` or `⏸️ [paused](url)`."""
    icon = RUN_ICONS.get(run["status"], "❔")
    label = RUN_LABELS.get(run["status"], "run log")
    return f"{icon} [{label}]({run['url']})" if run.get("url") else f"{icon} {label}"


# -- run comments -------------------------------------------------------------------


def find_run(comments, url):
    """Return (comment_id, run) for the run comment of the workflow run at `url`."""
    if url:
        for comment_id, run in _marked(comments, RUN_PATTERN):
            if run.get("url") == url:
                return comment_id, run
    return None, None


def run_comments(comments):
    """(comment, run) for each NexKit run comment, oldest first."""
    for comment in comments:
        if by_bot(comment):
            match = RUN_PATTERN.search(comment.get("body", ""))
            if match:
                yield comment, json.loads(base64.b64decode(match.group(1)))


def run_marker(run):
    return _encode(RUN_PREFIX, run)


def with_run(body, run, line=""):
    """A run comment's `body` with its marker replaced by `run`'s and `line` added to its
    text, for a change after the run ended."""
    text = RUN_PATTERN.sub("", body).rstrip()
    if line:
        text += f"\n\n{line}"
    return f"{text}\n\n{run_marker(run)}"


def next_round(comments):
    """The number for a new pull request round, counting rounds of v1.1.0 state comments."""
    numbers = [run.get("round") or 0 for _, run in _marked(comments, RUN_PATTERN)]
    for _, state in _marked(comments, STATE_PATTERN):
        numbers += [row.get("round") or 0 for row in state.get("rounds") or []]
    return max(numbers, default=0) + 1


def _cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_run(run, text=""):
    """A run comment: what started the run, its status and link, and the result text."""
    if "round" in run:
        cost = run.get("cost")
        cells = (
            (run.get("head") or "-")[:7],
            run.get("checks", "-"),
            run.get("verdict", "-"),
            f"${cost:.2f}" if isinstance(cost, (int, float)) else "-",
        )
        lines = [
            f"### NexKit round {run['round']}: {run['trigger']}",
            "",
            run_link(run),
            "",
            "| Commit | Checks | AI review | Cost |",
            "|---|---|---|---|",
            "| " + " | ".join(_cell(v) for v in cells) + " |",
        ]
    else:
        lines = [f"### NexKit `/nexkit {run['command']}`", "", run_link(run)]
    text = (text or "").strip()
    if text:
        lines += ["", text]
    return "\n".join(lines) + "\n\n" + _encode(RUN_PREFIX, run)


# -- pull request state -------------------------------------------------------------


def empty_state(issue):
    return {"version": 2, "issue": issue, "auto_fixes": 0, "feedback": None}


def read_state(comments):
    """Return (comment_id, state) for the PR's NexKit state comment, or (None, None).

    Pull requests from v1.1.0 have a state comment with a rounds table. It is read like
    any other, but `progress.save_state` leaves it unchanged and posts a new one."""
    return _find(comments, STATE_PATTERN)


def render_state(state):
    lines = [
        "### NexKit",
        "",
        f"Working on #{state['issue']}. Automatic fix rounds used: {state['auto_fixes']}. "
        "Each round has its own NexKit comment with a link to its run.",
        "",
        "Comment `/nexkit fix <instructions>` to request another round, or `/nexkit review` "
        "to re-check the current commit.",
    ]
    return "\n".join(lines) + "\n\n" + _encode(STATE_PREFIX, state)
