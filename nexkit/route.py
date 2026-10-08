"""Turn a GitHub event into one pipeline action, or a reasoned no-op."""

from __future__ import annotations

import re

from . import config as configuration
from .state import by_bot, latest_plan, plan_profile

BRANCH_PREFIX = "nexkit/issue-"
COMMAND = re.compile(r"^\s*/nexkit\s+([a-z]+)\b[ \t]*(.*)$", re.DOTALL)
ISSUE_COMMANDS = {"plan": "plan", "go": "implement"}
PR_COMMANDS = {"fix": "fix", "review": "review"}

USAGE = (
    "NexKit commands: on an issue, `/nexkit plan` drafts a plan and `/nexkit go` implements it; "
    "on a NexKit pull request, `/nexkit fix <instructions>` starts a repair round and "
    "`/nexkit review` re-checks the current commit."
)


def branch_for(issue):
    return f"{BRANCH_PREFIX}{issue}"


def issue_from_branch(branch):
    if branch and branch.startswith(BRANCH_PREFIX) and branch[len(BRANCH_PREFIX) :].isdigit():
        return int(branch[len(BRANCH_PREFIX) :])
    return None


def parse_command(body):
    """Return (command, note) for a comment whose first line is a /nexkit command."""
    match = COMMAND.match(body or "")
    if not match:
        return None, ""
    return match.group(1), match.group(2).strip()


def _none(reason, reply_to=None):
    return {"action": "none", "reason": reason, "reply_to": reply_to}


def route(gh, event_name, event, inputs=None):
    """Decide the action. The result is a JSON-serialisable dict.

    Every action needs a trusted actor: a user with write access, or a dispatch, which
    GitHub already restricts to writers and to this repository's own workflows.
    """
    inputs = inputs or {}
    if event_name == "issue_comment":
        if event.get("action") != "created" or by_bot(event["comment"]):
            return _none("Not a new human comment")
        if (event["comment"].get("user") or {}).get("type") == "Bot":
            return _none("Comments from bots are ignored")
        command, note = parse_command(event["comment"]["body"])
        if command is None:
            return _none("Comment is not a NexKit command")
        number = event["issue"]["number"]
        actor = event["comment"]["user"]["login"]
        if not gh.can_write(actor):
            return _none(f"@{actor} needs write access to run NexKit commands", number)
        on_pull = "pull_request" in event["issue"]
        decision = _decide(gh, command, number, on_pull, note, auto=False)
        decision.update(actor=actor, comment_id=event["comment"]["id"])
        return decision

    if event_name == "pull_request_review":
        review = event["review"]
        if event.get("action") != "submitted" or review.get("state") != "changes_requested":
            return _none("Only 'Request changes' reviews start a fix round")
        actor = review["user"]["login"]
        if review["user"].get("type") == "Bot" or not gh.can_write(actor):
            return _none(f"Review by @{actor} cannot start a fix round")
        decision = _decide(
            gh, "fix", event["pull_request"]["number"], True, review.get("body") or "", auto=False
        )
        decision["actor"] = actor
        return decision

    if event_name == "workflow_dispatch":
        command = (inputs.get("command") or "").strip()
        number = str(inputs.get("number") or "").strip()
        if not number.isdigit():
            return _none("Dispatch input 'number' must be an issue or PR number")
        issue = gh.issue(int(number))
        on_pull = "pull_request" in issue
        auto = str(inputs.get("auto", "")).lower() == "true"
        decision = _decide(gh, command, int(number), on_pull, inputs.get("note") or "", auto=auto)
        decision["actor"] = event.get("sender", {}).get("login")
        return decision

    return _none(f"Event '{event_name}' is not handled")


def _decide(gh, command, number, on_pull, note, *, auto):
    if on_pull:
        if command not in PR_COMMANDS:
            return _none(f"`/nexkit {command}` is not a pull request command. {USAGE}", number)
        pull = gh.pull(number)
        issue = issue_from_branch(pull["head"]["ref"])
        if issue is None or pull["head"]["repo"]["full_name"] != gh.repository:
            return _none("This pull request was not opened by NexKit", number)
        if pull["state"] != "open":
            return _none("The pull request is closed", number)
        return {
            "action": PR_COMMANDS[command],
            "issue": issue,
            "pr": number,
            "target": number,
            "branch": pull["head"]["ref"],
            "base": pull["base"]["ref"],
            "head": pull["head"]["sha"],
            "note": note,
            "auto": auto,
        }

    if command not in ISSUE_COMMANDS:
        return _none(f"`/nexkit {command}` is not an issue command. {USAGE}", number)
    issue = gh.issue(number)
    if issue["state"] != "open":
        return _none("The issue is closed", number)
    branch = branch_for(number)
    action = ISSUE_COMMANDS[command]
    if action == "implement":
        existing = gh.open_pulls(branch)
        if existing:
            pr = existing[0]["number"]
            return _none(
                f"Pull request #{pr} is already open for this issue. Use `/nexkit fix` there, "
                "or close it to start over.",
                number,
            )
    return {
        "action": action,
        "issue": number,
        "pr": None,
        "target": number,
        "branch": branch,
        "base": gh.default_branch(),
        "head": None,
        "note": note,
        "auto": False,
    }


def with_profile(gh, decision, cfg):
    """Return the decision and configuration for the run's profile, or a refusal.

    The profile comes from the latest plan. `/nexkit plan` gets it as `previous_profile`,
    because triage chooses this plan's profile in the agent job. The other actions use it,
    or `default_profile` when no plan names one. They never fall back from a profile that
    is no longer configured, because that could move a hard issue to a weaker model.
    """
    if not cfg["profiles"] or decision["action"] == "none":
        return decision, cfg
    issue = decision["issue"]
    plan = latest_plan(gh.comments(issue))
    record = plan_profile(plan) if plan else None
    name = record["profile"] if record else None
    if decision["action"] == "plan":
        # A profile that was only the fallback after a failed triage is not a choice to keep.
        kept = name in cfg["profiles"] and record.get("chosen_by") != "default"
        return {**decision, "previous_profile": name if kept else None}, cfg
    name = name or cfg["default_profile"]
    if name not in cfg["profiles"]:
        reason = (
            f"The plan on #{issue} uses the profile `{name}`, which is no longer in "
            f".nexkit/config.json. Comment `/nexkit plan` on #{issue} to plan it again."
        )
        return _none(reason, decision["target"]), cfg
    return {**decision, "profile": name}, configuration.with_profile(cfg, name)
