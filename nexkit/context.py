"""Collect the GitHub material an agent stage needs, before the agent runs.

Gathering happens in its own step so the agent process never holds a GitHub token.
"""

from __future__ import annotations

import re

from .github import TRUSTED_ASSOCIATIONS
from .route import parse_command
from .state import by_bot, clip, latest_plan, plan_text, read_state

MAX_COMMENTS = 30
NO_PLAN = "No plan was posted. Implement the issue as described."
# The section of a NexKit pull request description that `publish.pull_body` writes from the
# implement result's `outside_plan`.
OUTSIDE_PLAN = re.compile(r"^## Outside the plan\n(.*?)(?=^## |^---$|\Z)", re.M | re.S)


def _trusted(item):
    return item.get("author_association") in TRUSTED_ASSOCIATIONS and not by_bot(item)


def _discussion(comments, notes=False):
    """Collaborators' comments. Command comments are left out, or with `notes` kept when
    they carry a note: the agent that acts on a command gets its note through the
    decision, the reviewer only through the discussion."""
    entries = []
    for comment in comments:
        if not _trusted(comment):
            continue
        command, note = parse_command(comment.get("body"))
        if command and not (notes and note):
            continue
        entries.append(
            f"@{comment['user']['login']} ({comment['created_at']}):\n{clip(comment['body'])}"
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
                f"{clip(check.get('output'), 6000)}\n```"
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
            human.append(f"@{review['user']['login']} ({review['state']}): {clip(review['body'])}")
    for comment in gh.review_comments(pr):
        if _trusted(comment) and comment.get("line") is not None:
            human.append(
                f"@{comment['user']['login']} on `{comment['path']}:{comment['line']}`: "
                f"{clip(comment['body'])}"
            )
    if human:
        parts.append("### Review comments from collaborators\n\n" + "\n\n".join(human))
    return "\n\n".join(parts) if parts else "No recorded feedback. Follow the note."


def previous_review(gh, decision, fix_result):
    """The last NexKit review of the pull request and the fix rounds since, or None."""
    _, state = read_state(gh.comments(decision["pr"]))
    last = (state or {}).get("last_review")
    if not last:
        return None
    fixes = [f for f in state.get("fixes") or [] if f["round"] > last["round"]]
    if _fixed(decision, fix_result):
        fixes.append(
            {
                "auto": decision.get("auto"),
                "summary": fix_result.get("summary"),
                "note": decision.get("note"),
                "conflicts": fix_result.get("conflicts") if fix_result.get("start_base") else None,
            }
        )
    lines = [f"### Findings of the review of commit {last['head'][:7]} ({last['verdict']})", ""]
    for number, f in enumerate(last["findings"], 1):
        where = f"{f['file']}:{f['line']}" if f.get("file") else "general"
        lines.append(f"{number}. {f['severity']}, `{where}`: {clip(f['body'], 1500)}")
    if not last["findings"]:
        lines.append("No findings.")
    lines += ["", "### Fix rounds since that review"]
    if not fixes:
        lines += ["", "None. Any later commits were pushed without a NexKit fix round."]
    for number, fix in enumerate(fixes, 1):
        who = "automatic" if fix.get("auto") else "requested by a person"
        lines += [
            "",
            f"#### Fix {number} ({who})",
            "",
            "Note from the person who requested it:",
            "",
            clip(fix.get("note")) or "None.",
            "",
            "Summary from the fix agent:",
            "",
            clip(fix.get("summary")) or "None.",
        ]
        if fix.get("conflicts"):
            files = ", ".join(f"`{name}`" for name in fix["conflicts"])
            lines += ["", f"This round merged the base branch and resolved conflicts in {files}."]
    return {"previous_head": last["head"], "previous_round": "\n".join(lines)}


def outside_plan(pull):
    """What the implementation agent did outside the plan, and why, from the pull request
    description that NexKit wrote."""
    body = ((pull or {}).get("body") or "").replace("\r\n", "\n")
    match = OUTSIDE_PLAN.search(body)
    return clip(match.group(1)) if match and match.group(1).strip() else "None."


def _fixed(decision, fix_result):
    return decision["action"] == "fix" and (fix_result or {}).get("status") == "done"


def gather(gh, decision, stage=None, fix_result=None):
    """Context for an agent stage. `stage` defaults to the decision's action; the review
    stage also gets the previous review round, with `fix_result` from this run's fix."""
    issue = gh.issue(decision["issue"])
    comments = gh.comments(decision["issue"])
    plan = latest_plan(comments)
    context = {
        "issue": decision["issue"],
        "pr": decision.get("pr") or "",
        "title": issue["title"],
        "body": clip(issue.get("body") or "No description.", 20000),
        "discussion": _discussion(comments),
        "plan": plan_text(plan) if plan else NO_PLAN,
        "note": decision.get("note") or "None.",
        "feedback": "",
        "outside_plan": "None.",
    }
    stage = stage or decision["action"]
    if stage == "review" and decision.get("pr"):
        context["discussion"] = _discussion(comments, notes=True)
        pr_comments = gh.comments(decision["pr"])
        context["discussion"] += "\n\n---\n\nOn the pull request:\n\n" + _discussion(
            pr_comments, notes=True
        )
        context["outside_plan"] = outside_plan(gh.pull(decision["pr"]))
        context.update(previous_review(gh, decision, fix_result) or {})
        if _fixed(decision, fix_result) and fix_result.get("start_base"):
            context["merged"] = {
                "head": fix_result.get("start_head"),
                "base": fix_result["start_base"],
                "conflicts": fix_result.get("conflicts") or [],
            }
    elif stage == "fix":
        context["feedback"] = _feedback(gh, decision)
        pr_comments = gh.comments(decision["pr"])
        context["discussion"] += "\n\n---\n\nOn the pull request:\n\n" + _discussion(pr_comments)
    return context
