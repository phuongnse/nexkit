"""Collect the GitHub material an agent stage needs, before the agent runs.

Gathering happens in its own step so the agent process never holds a GitHub token.
"""

from __future__ import annotations

from .github import TRUSTED_ASSOCIATIONS
from .route import parse_command
from .state import by_bot, latest_plan, plan_text, read_state

MAX_COMMENT = 4000
MAX_COMMENTS = 30
NO_PLAN = "No plan was posted. Implement the issue as described."


def _clip(text, limit=MAX_COMMENT):
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "\n[truncated]"


def _trusted(item):
    return item.get("author_association") in TRUSTED_ASSOCIATIONS and not by_bot(item)


def _discussion(comments):
    entries = []
    for comment in comments:
        if not _trusted(comment):
            continue
        command, _ = parse_command(comment.get("body"))
        if command:
            continue  # Command notes reach the agent through the decision instead.
        entries.append(
            f"@{comment['user']['login']} ({comment['created_at']}):\n{_clip(comment['body'])}"
        )
    entries = entries[-MAX_COMMENTS:]
    return "\n\n---\n\n".join(entries) if entries else "No discussion."


def _feedback(gh, decision):
    """Earlier NexKit results plus current human review feedback on the pull request."""
    pr = decision["pr"]
    head = decision["head"]
    parts = []
    _, state = read_state(gh.comments(pr))
    saved = (state or {}).get("feedback") or {}
    for check in saved.get("checks") or []:
        if not check.get("passed"):
            parts.append(
                f"### Failing check `{check['name']}` (`{check['run']}`)\n\n"
                f"Exit code {check.get('exit_code')}. End of output:\n\n```\n"
                f"{_clip(check.get('output'), 6000)}\n```"
            )
    review = saved.get("review") or {}
    findings = [f for f in review.get("findings") or [] if f.get("severity") == "blocking"]
    if findings:
        lines = ["### Blocking findings from the NexKit review"]
        for f in findings:
            where = f"{f.get('file')}:{f.get('line')}" if f.get("file") else "general"
            lines.append(f"- `{where}`: {f.get('body')}")
        parts.append("\n".join(lines))

    human = []
    for review in gh.reviews(pr):
        if _trusted(review) and review.get("commit_id") == head and (review.get("body") or ""):
            human.append(f"@{review['user']['login']} ({review['state']}): {_clip(review['body'])}")
    for comment in gh.review_comments(pr):
        if _trusted(comment) and comment.get("line") is not None:
            human.append(
                f"@{comment['user']['login']} on `{comment['path']}:{comment['line']}`: "
                f"{_clip(comment['body'])}"
            )
    if human:
        parts.append("### Review comments from collaborators\n\n" + "\n\n".join(human))
    return "\n\n".join(parts) if parts else "No recorded feedback. Follow the note."


def gather(gh, decision):
    issue = gh.issue(decision["issue"])
    comments = gh.comments(decision["issue"])
    plan = latest_plan(comments)
    context = {
        "issue": decision["issue"],
        "pr": decision.get("pr") or "",
        "title": issue["title"],
        "body": _clip(issue.get("body") or "No description.", 20000),
        "discussion": _discussion(comments),
        "plan": plan_text(plan) if plan else NO_PLAN,
        "note": decision.get("note") or "None.",
        "feedback": "",
    }
    if decision["action"] == "fix":
        context["feedback"] = _feedback(gh, decision)
        pr_comments = gh.comments(decision["pr"])
        context["discussion"] += "\n\n---\n\nOn the pull request:\n\n" + _discussion(pr_comments)
    return context
