"""`nexkit status`: the open NexKit work in a repository and what each item waits for.

Read-only. States come from the hidden markers in NexKit's comments (run comments, plan
comments) and from GitHub's own pull request data, never from the text shown to people.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from .config import validate
from .route import issue_from_branch
from .state import (
    BOT_LOGIN,
    FAILURE,
    PAUSED,
    RUNNING,
    SUCCESS,
    latest_plan,
    plan_profile,
    run_comments,
)

MERGES = 5
WORKERS = 8
# Pull request states, in the order a person reads them.
PULL_STATES = {
    "running": "a round is running",
    "paused": "paused at the Claude usage limit",
    "ready": "ready for a person",
    "merge_refused": "the automatic merge was refused",
    "conflicting": "conflicts with its base branch",
    "blocked": "the agent is blocked",
    "failing": "checks or review failing",
    "failed": "the last round failed",
}
ISSUE_STATES = {
    "planning": "a plan is being written",
    "planned": "planned",
    "implementing": "being implemented",
    "paused": "paused at the Claude usage limit",
    "failed": "the last run failed",
    "in_pull_request": "has an open pull request",
    "implemented": "implemented; no open pull request",
}
# The round started the next one by itself, which is queued or running.
QUEUED = {"auto_fix", "conflict_fix"}


def _runs(comments):
    """[(comment, run)] for NexKit's run comments, oldest first."""
    return list(run_comments(comments))


def _link(comment):
    return comment.get("html_url") or ""


def issue_item(issue, comments, open_pulls):
    """The status of an open issue that NexKit worked on, or None."""
    number = issue["number"]
    runs = [(c, run) for c, run in _runs(comments) if "command" in run]
    plan = latest_plan(comments)
    item = {"number": number, "title": issue.get("title", ""), "url": issue.get("html_url", "")}
    if plan:
        record = plan_profile(plan) or {}
        item["plan"] = {"url": _link(plan), "profile": record.get("profile")}
    if not runs:
        if not plan:
            return None
        return {**item, "state": "planned", "link": _link(plan)}
    comment, run = runs[-1]
    item.update(link=_link(comment), run=run.get("url"), command=run.get("command"))
    status, command = run.get("status"), run.get("command")
    if run.get("attention"):
        item["attention"] = run["attention"]
    if status == RUNNING:
        item["state"] = "planning" if command == "plan" else "implementing"
    elif status == PAUSED:
        item["state"] = "paused"
        item["resume_at"] = (run.get("resume") or {}).get("at")
    elif status != SUCCESS:
        item["state"] = "failed"
        item.setdefault("attention", "the last run failed")
    elif command == "plan":
        item.update(state="planned", link=(item.get("plan") or {}).get("url") or item["link"])
    elif number in open_pulls:
        item.update(state="in_pull_request", pull=open_pulls[number])
        item.pop("attention", None)  # the pull request's own item says what it needs
    else:
        item["state"] = "implemented"
    return item


def _meaningful(rounds):
    """The latest round that says something about the pull request: a round that found
    nothing to merge does not."""
    for comment, run in reversed(rounds):
        if run.get("outcome") != "up_to_date":
            return comment, run
    return rounds[-1]


def pull_state(run, conflicting):
    """A pull request's state from its latest round and GitHub's mergeability."""
    status, outcome = run.get("status"), run.get("outcome")
    if status == RUNNING or outcome in QUEUED:
        return "running"
    if status == PAUSED:
        return "paused"
    if conflicting:
        return "conflicting"
    if status == FAILURE:
        return "blocked" if outcome == "agent_blocked" else "failed"
    if outcome in ("ready", "merge_refused"):
        return outcome
    if outcome is None:
        # Rounds from releases before outcomes were recorded.
        passed = run.get("checks") == "passed" and run.get("verdict") == "approve"
        return "ready" if passed else "failing"
    return "failing"


def pull_item(pull, comments, cfg):
    """The status of an open NexKit pull request."""
    number = pull["number"]
    rounds = [(c, run) for c, run in _runs(comments) if "round" in run]
    mergeable = pull.get("mergeable")
    item = {
        "number": number,
        "issue": issue_from_branch(pull["head"]["ref"]),
        "title": pull.get("title", ""),
        "url": pull.get("html_url", ""),
        "conflicting": mergeable is False,
    }
    if not rounds:
        item["state"] = "conflicting" if mergeable is False else "failing"
        return item
    comment, run = rounds[-1]
    _, meaningful = _meaningful(rounds)
    state = pull_state(meaningful if run.get("status") == SUCCESS else run, mergeable is False)
    item.update(
        state=state,
        round=run.get("round"),
        trigger=run.get("trigger"),
        link=_link(comment),
        run=run.get("url"),
    )
    if state == "paused":
        item["resume_at"] = (run.get("resume") or {}).get("at")
    attention = (meaningful if run.get("status") == SUCCESS else run).get("attention")
    if state == "conflicting" and not cfg["conflicts"]["auto_resolve"]:
        attention = f"conflicts with `{pull['base']['ref']}`; comment `/nexkit fix`"
    elif state in ("running", "conflicting"):
        attention = None
    elif not attention and state in ("failed", "blocked"):
        attention = "the last round failed"
    elif not attention and state == "ready" and not cfg["merge"]["auto"]:
        attention = "the pull request is ready for a human decision"
    if attention:
        item["attention"] = attention
    return item


def _workflow_state(gh, record):
    if record.get("error"):
        return "not started"
    if not record.get("run_id"):
        return "unknown"
    try:
        run = gh.workflow_run(record["run_id"])
    except Exception:  # noqa: BLE001 - a missing run must not stop the listing
        return "unknown"
    return run.get("conclusion") or run.get("status") or "unknown"


def merge_item(gh, pull, comments):
    """A pull request NexKit merged, with the state of each after-merge workflow."""
    rounds = [run for _, run in _runs(comments) if "round" in run]
    merged = next((run for run in reversed(rounds) if run.get("outcome") == "merged"), None)
    by_nexkit = merged is not None or (pull.get("merged_by") or {}).get("login") == BOT_LOGIN
    if not by_nexkit:
        return None
    workflows = [
        {"workflow": r.get("workflow"), "url": r.get("url"), "state": _workflow_state(gh, r)}
        for r in (merged or {}).get("after_merge") or []
    ]
    return {
        "number": pull["number"],
        "issue": issue_from_branch(pull["head"]["ref"]),
        "title": pull.get("title", ""),
        "url": pull.get("html_url", ""),
        "merged_at": pull.get("merged_at"),
        "workflows": workflows,
    }


def _nexkit_pull(gh, pull):
    head = pull.get("head") or {}
    repo = (head.get("repo") or {}).get("full_name")
    return issue_from_branch(head.get("ref")) is not None and repo == gh.repository


def collect(gh, cfg=None, merges=MERGES, workers=WORKERS):
    """Everything `nexkit status` shows, as a JSON-serialisable dict."""
    cfg = cfg or validate({})
    with ThreadPoolExecutor(max_workers=workers) as pool:
        listed = list(gh.open_issues())
        pulls = [p for p in gh.list_pulls("open") if _nexkit_pull(gh, p)]
        closed = [
            p
            for p in gh.list_pulls("closed", limit=100)
            if p.get("merged_at") and _nexkit_pull(gh, p)
        ]
        closed.sort(key=lambda p: p["merged_at"], reverse=True)
        pull_numbers = {p["number"] for p in pulls}
        issues = [
            i
            for i in listed
            if "pull_request" not in i and i["number"] not in pull_numbers and i.get("comments", 1)
        ]
        open_pulls = {issue_from_branch(p["head"]["ref"]): p["number"] for p in pulls}
        # Pull details hold `mergeable`; GitHub may still be working it out (null).
        details = pool.map(lambda p: {**p, **gh.pull(p["number"])}, pulls)
        pull_comments = pool.map(lambda p: gh.comments(p["number"]), pulls)
        issue_comments = pool.map(lambda i: gh.comments(i["number"]), issues)
        pull_items = [pull_item(p, c, cfg) for p, c in zip(details, pull_comments, strict=True)]
        issue_items = [
            issue_item(i, c, open_pulls) for i, c in zip(issues, issue_comments, strict=True)
        ]
        candidates = closed[: merges * 2]
        merge_details = pool.map(lambda p: {**p, **gh.pull(p["number"])}, candidates)
        merge_comments = pool.map(lambda p: gh.comments(p["number"]), candidates)
        merge_items = [
            merge_item(gh, p, c) for p, c in zip(merge_details, merge_comments, strict=True)
        ]
    issue_items = sorted((i for i in issue_items if i), key=lambda i: i["number"])
    pull_items.sort(key=lambda p: p["number"])
    needs = [
        {"number": item["number"], "kind": kind, "reason": item["attention"]}
        for kind, items in (("issue", issue_items), ("pull_request", pull_items))
        for item in items
        if item.get("attention")
    ]
    return {
        "repository": gh.repository,
        "issues": issue_items,
        "pull_requests": pull_items,
        "recent_merges": [m for m in merge_items if m][:merges],
        "needs_person": needs,
    }


def _short(text, width=50):
    text = " ".join(str(text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def render(data):
    """The plain text form: one line per item, then what needs a person."""
    lines = [f"NexKit status for {data['repository']}"]
    lines += ["", "Issues"]
    for item in data["issues"]:
        detail = ""
        if item["state"] == "planned" and (item.get("plan") or {}).get("profile"):
            detail = f" (profile {item['plan']['profile']})"
        elif item["state"] == "in_pull_request":
            detail = f" (#{item['pull']})"
        elif item["state"] == "paused" and item.get("resume_at"):
            detail = f" (resumes after {item['resume_at']})"
        lines.append(
            f"  #{item['number']:<5} {item['state']:<16} {_short(item['title'])}{detail}"
            f"  {item.get('link') or item['url']}"
        )
    if not data["issues"]:
        lines.append("  none")
    lines += ["", "Pull requests"]
    for item in data["pull_requests"]:
        detail = f" round {item['round']} ({item['trigger']})" if item.get("round") else ""
        if item["state"] == "paused" and item.get("resume_at"):
            detail += f", resumes after {item['resume_at']}"
        lines.append(
            f"  #{item['number']:<5} {item['state']:<16} {_short(item['title'])}{detail}"
            f"  {item.get('link') or item['url']}"
        )
    if not data["pull_requests"]:
        lines.append("  none")
    lines += ["", "Recent merges by NexKit"]
    for item in data["recent_merges"]:
        workflows = ", ".join(f"{w['workflow']}: {w['state']}" for w in item["workflows"])
        lines.append(
            f"  #{item['number']:<5} {item['merged_at']}  {_short(item['title'])}"
            + (f"  [{workflows}]" if workflows else "")
        )
    if not data["recent_merges"]:
        lines.append("  none")
    lines += ["", "Needs a person"]
    for item in data["needs_person"]:
        lines.append(f"  #{item['number']:<5} {item['reason']}")
    if not data["needs_person"]:
        lines.append("  nothing")
    return "\n".join(lines)


def to_json(data):
    return json.dumps(data, indent=2)
