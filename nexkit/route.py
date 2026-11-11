"""Turn a GitHub event into one pipeline action, or a reasoned no-op."""

from __future__ import annotations

import posixpath
import re

from . import config as configuration
from .github import GitHubError
from .globs import unaffected
from .state import (
    FAILURE,
    RUNNING,
    SUCCESS,
    approved_head,
    asked_last,
    by_bot,
    check_fingerprint,
    check_heads,
    closing_answer,
    latest_plan,
    plan_profile,
    repeated_failures,
    results_run,
    run_comments,
    setup_fingerprint,
    verified_head,
)

BRANCH_PREFIX = "nexkit/issue-"
COMMAND = re.compile(r"^\s*/nexkit\s+([a-z]+)\b[ \t]*(.*)$", re.DOTALL)
ISSUE_COMMANDS = {"plan": "plan", "go": "implement"}
PR_COMMANDS = {"fix": "fix", "review": "review"}
# A writer's word for an issue that autopilot leaves alone (see `autopilot.distrust`). Its
# comment is the record, so only a comment counts, never a dispatch.
TRUST = "trust"

USAGE = (
    "NexKit commands: on an issue, `/nexkit plan` drafts a plan, `/nexkit go` implements it and "
    "`/nexkit trust` vouches for it, so that autopilot takes it; on a NexKit pull request, "
    "`/nexkit fix <instructions>` starts a repair round; on any pull request of this "
    "repository's branches, `/nexkit review` checks and reviews the current commit."
)


# The run id in a run comment's link.
RUN_ID = re.compile(r"/actions/runs/(\d+)")


def branch_for(issue):
    return f"{BRANCH_PREFIX}{issue}"


def implementing(gh, issue):
    """The run of `/nexkit go` that still runs on `issue`, or None. It opened the issue's
    pull request and checks, reviews and merges it before it ends, so it is a round of that
    pull request too. A run whose workflow run ended without a report does not count, nor
    one whose workflow run cannot be read, so a lost run never holds the pull request up."""
    runs = [run for _, run in run_comments(gh.comments(issue)) if run.get("command") == "go"]
    if not runs or runs[-1].get("status") != RUNNING:
        return None
    match = RUN_ID.search(runs[-1].get("url") or "")
    if not match:
        return None
    try:
        if gh.workflow_run(int(match.group(1))).get("status") == "completed":
            return None
    except GitHubError:
        return None
    return runs[-1]


def _one_round(gh, decision):
    """A person's command on a pull request whose implement round still runs: a refusal,
    since a pull request runs one NexKit round at a time; otherwise `decision`."""
    if not decision.get("pr"):
        return decision
    running = implementing(gh, decision["issue"])
    if not running:
        return decision
    run = f"[implement round]({running['url']})" if running.get("url") else "implement round"
    return _none(
        f"The {run} on #{decision['issue']} still checks and reviews this pull request, and "
        "a pull request runs one NexKit round at a time. Comment the command again once it "
        "ends.",
        decision["pr"],
    )


def issue_from_branch(branch):
    if branch and branch.startswith(BRANCH_PREFIX) and branch[len(BRANCH_PREFIX) :].isdigit():
        return int(branch[len(BRANCH_PREFIX) :])
    return None


# The `auto` dispatch input: `true` for an automatic fix round after failures, `conflicts`
# for an automatic round that merges the base branch, `resume` for a command that NexKit
# runs again after the agent's usage limit reset, `autopilot` for a command that autopilot
# started, `retry` for the first run again after a transient error and `retry:N` for the
# N-th (see `retry.wait_minutes`), `carry` for the review after NexKit brought an approved
# pull request up to date with its base branch, which carries the approval over when the
# change is the same. Several are joined with commas.
RESUME = "resume"
RETRY = "retry"
RETRY_KIND = re.compile(r"retry(?::(\d{1,2}))?")
CARRY = "carry"
# Also the `command` of the dispatch that starts an autopilot sweep.
AUTOPILOT = "autopilot"
# The note of a sweep that waits for a workflow run to end first, and how route reads it.
AFTER_RUN = "after run {}"
AFTER_RUN_PATTERN = re.compile(r"after run (\d+)")
# Issue events after which an issue may have come into autopilot's scope, or a label of
# `autopilot.priority` or `autopilot.labels` may have changed what it does.
AUTOPILOT_EVENTS = ("opened", "reopened", "milestoned", "labeled", "unlabeled")


def auto_kinds(value):
    kinds = {kind.strip() for kind in str(value or "").lower().split(",")}
    return {"resumed" if kind == RESUME else kind for kind in kinds if kind}


def retry_number(kinds):
    """Which run again after a transient error the `auto` kinds name: 1 for `retry`, N for
    `retry:N`, 0 for none."""
    for kind in kinds:
        match = RETRY_KIND.fullmatch(kind)
        if match:
            return max(1, int(match.group(1) or 1))
    return 0


def retry_kind(number):
    """The `auto` kind of the `number`-th run again after a transient error."""
    return RETRY if number <= 1 else f"{RETRY}:{number}"


def auto_input(decision, resume=False, retry=0):
    """The `auto` input that starts this decision's command again: with `retry`, as that
    run again after a transient error; otherwise as the run again the decision was."""
    kinds = ["conflicts" if decision.get("conflicts") else "true"] if decision.get("auto") else []
    kinds += [AUTOPILOT] if decision.get(AUTOPILOT) else []
    kinds += [RESUME] if resume else []
    number = int(retry or decision.get(RETRY) or 0)
    kinds += [retry_kind(number)] if number else []
    kinds += [CARRY] if decision.get("carry_from") else []
    return ",".join(kinds) or "false"


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
        if command == TRUST and not on_pull:
            decision = _trust(event["issue"], note)
        else:
            decision = _one_round(gh, _decide(gh, command, number, on_pull, note, auto=False))
            decision = _closing(gh, decision)
        decision.update(actor=actor, comment_id=event["comment"]["id"])
        return decision

    if event_name == "pull_request_review":
        review = event["review"]
        state = review.get("state")
        if event.get("action") != "submitted" or state not in ("changes_requested", "approved"):
            return _none("Only 'Request changes' reviews and approvals start anything")
        actor = review["user"]["login"]
        if review["user"].get("type") == "Bot" or not gh.can_write(actor):
            return _none(f"Review by @{actor} cannot start anything")
        if state == "approved":
            return _approved(event["pull_request"], actor, review.get("commit_id"))
        decision = _decide(
            gh, "fix", event["pull_request"]["number"], True, review.get("body") or "", auto=False
        )
        decision = _one_round(gh, decision)
        decision["actor"] = actor
        return decision

    if event_name == "issues":
        # Anyone who may close the issue starts this; it only closes parents whose
        # sub-issues are all closed, with `close_parent_issues` on, and starts an
        # autopilot sweep, with `autopilot.enabled` on.
        action = event.get("action")
        if action == "closed":
            return {"action": "maintain", "task": "parents", "issue": event["issue"]["number"]}
        if action in AUTOPILOT_EVENTS:
            # Anyone who may open an issue starts this, so it only starts a sweep, which
            # takes only issues from people with write access.
            return {"action": "maintain", "task": "autopilot", "event": action}
        return _none(f"Issue event '{action}' is not handled")

    if event_name == "milestone":
        if event.get("action") != "closed":
            return _none("Only closed milestones are handled")
        return {"action": "maintain", "task": "autopilot", "event": "milestone_closed"}

    if event_name == "schedule":
        return {"action": "maintain", "task": "schedule"}

    if event_name == "push":
        # A person or NexKit pushed or merged into a branch.
        branch = (event.get("ref") or "").removeprefix("refs/heads/")
        if branch != (event.get("repository") or {}).get("default_branch"):
            return _none("Only pushes to the default branch are handled")
        return {"action": "maintain", "task": "conflicts", "base": branch}

    if event_name == "workflow_run":
        # A run of another workflow finished. Maintain starts a sweep when it is a run of a
        # workflow in merge.after_workflows or autopilot.watch_workflows on the default
        # branch that a sweep acts on.
        run = event.get("workflow_run") or {}
        if event.get("action") != "completed" or not isinstance(run.get("id"), int):
            return _none("Only finished workflow runs are handled")
        workflow = posixpath.basename(str(run.get("path") or ""))
        return {"action": "maintain", "task": "ci", "workflow": workflow, "run": run["id"]}

    if event_name == "workflow_dispatch":
        command = (inputs.get("command") or "").strip()
        number = str(inputs.get("number") or "").strip()
        if command == "resume":
            # NexKit's own run that waits for commands paused at the usage limit.
            return {"action": "maintain", "task": "resume"}
        if command == AUTOPILOT:
            # A sweep over the issues in scope. Its note names the issue whose closing
            # started it, or the run whose report did not finish, if any.
            decision = {"action": "maintain", "task": "sweep"}
            note = str(inputs.get("note") or "").strip()
            after = AFTER_RUN_PATTERN.fullmatch(note)
            if note.isdigit():
                decision["closed"] = int(note)
            elif after:
                decision["after"] = int(after.group(1))
            return decision
        if not number.isdigit():
            return _none("Dispatch input 'number' must be an issue or PR number")
        issue = gh.issue(int(number))
        on_pull = "pull_request" in issue
        kinds = auto_kinds(inputs.get("auto"))
        auto = bool(kinds & {"true", "conflicts"})
        decision = _decide(gh, command, int(number), on_pull, inputs.get("note") or "", auto=auto)
        decision["actor"] = event.get("sender", {}).get("login")
        if decision["action"] not in ("none", "maintain"):
            decision.update(
                {kind: True for kind in ("conflicts", "resumed", AUTOPILOT) if kind in kinds}
            )
            if retry_number(kinds):
                # Which run again after a transient error this is, for the next wait.
                decision[RETRY] = retry_number(kinds)
        if decision["action"] == "review" and CARRY in kinds:
            approved = approved_head(gh.comments(decision["pr"]))
            if approved and approved != decision["head"]:
                # The review job carries this approval over when the change is the same.
                decision["carry_from"] = approved
        if decision.get("conflicts"):
            # Which base commit this round resolves, so the same conflict is tried once.
            decision["base_sha"] = gh.branch_sha(decision["base"])
            # A conflict round merges the base branch; it diagnoses nothing.
            decision.pop("diagnose", None)
            if _conflicts_tried(gh, decision):
                return _none(
                    f"A conflict round already ran on `{decision['base']}` at "
                    f"`{decision['base_sha'][:7]}`; this one, started at the same time, "
                    "has nothing to add"
                )
        return decision

    return _none(f"Event '{event_name}' is not handled")


def _approved(pull, actor, commit=None):
    """A writer's approval of the commit `commit` of a NexKit pull request: the `maintain`
    job merges it when its latest round could not, and accepts the checklist items a
    person decides (see `maintain.merge_approved`). It starts no agent, and runs in a
    concurrency group of its own, so it never replaces a command that waits."""
    issue = issue_from_branch((pull.get("head") or {}).get("ref"))
    repo = ((pull.get("head") or {}).get("repo") or {}).get("full_name")
    base = (pull.get("base") or {}).get("repo") or {}
    if issue is None or (base.get("full_name") and repo != base["full_name"]):
        return _none("Only an approval of a NexKit pull request starts a merge")
    return {
        "action": "maintain",
        "task": "approved",
        "pr": pull["number"],
        "issue": issue,
        "actor": actor,
        "commit": commit,
    }


def _closing(gh, decision):
    """A person's `/nexkit plan` or `/nexkit go` whose note picks the option, of the
    questions NexKit asked last on the issue, that only closes it: the `maintain` job
    closes the issue as not planned, and no agent runs, since another round could only
    stop at the same place. Any other note goes to its round as it is."""
    if decision["action"] not in ("plan", "implement") or not decision.get("note"):
        return decision
    asked = asked_last(gh.comments(decision["issue"]))
    option = closing_answer(decision["note"], asked[1]) if asked else None
    if not option or gh.open_pulls(decision["branch"]):
        # A pull request that is open stays with its issue: a person closes both.
        return decision
    return {
        "action": "maintain",
        "task": "close_answer",
        "issue": decision["issue"],
        "answer": option,
        "asked": asked[0],
    }


def _trust(issue, note):
    """A writer's `/nexkit trust` on `issue`: the `maintain` job answers it and starts a
    sweep, which reads the comment. With a note it vouches for nothing, since words after
    the command can say the opposite."""
    number = issue["number"]
    if issue.get("state", "open") != "open":
        return _none("The issue is closed", number)
    if note:
        reason = "`/nexkit trust` takes no note. Comment it alone once you read the issue."
        return _none(reason, number)
    return {"action": "maintain", "task": "trust", "issue": number, "reply_to": number}


def _conflicts_tried(gh, decision):
    """Whether this conflict round repeats the pull request's latest round: a conflict
    round on the same base commit that ended. Two starters that looked at the same time,
    such as a push to the default branch and a sweep, can each start one; the second
    waits in the pull request's concurrency group and then has nothing to add. A round
    that resumes or retries one, or starts once more after one ended without a report,
    goes on."""
    if decision.get("resumed") or decision.get(RETRY):
        return False
    rounds = [run for _, run in run_comments(gh.comments(decision["pr"])) if "round" in run]
    latest = rounds[-1] if rounds else {}
    return (
        bool(latest.get("conflicts"))
        and latest.get("base_sha") == decision["base_sha"]
        and latest.get("status") in (SUCCESS, FAILURE)
        and latest.get("outcome") != "lost"
    )


def on_base(gh, base, head):
    """The merge base of a pull request at `head` that changes no file against `base` any
    more, such as after a merge of `base` that brought the same change; otherwise None,
    also when GitHub cannot say."""
    try:
        found = gh.compare(base, head)
    except GitHubError:
        return None
    files = found.get("files") if isinstance(found, dict) else None
    if not isinstance(files, list) or files:
        return None
    return (found.get("merge_base_commit") or {}).get("sha") or head


# The most files GitHub lists for a pull request (see `GitHub.pull_files`).
PULL_FILES_LIMIT = 300


def head_config_error(gh, number, base, head):
    """Why NexKit would refuse the configuration that the pull request `number` brings at
    `head`, or "" when it leaves `.nexkit/config.json` as it is or the file loads. NexKit
    reads its configuration from the default branch, so a file it refuses would stop every
    command once it merged, its fix included. The file is read through the API and loaded
    by NexKit's own validation, as `nexkit doctor` loads it: nothing of the head runs, and
    the round's checks still come from the default branch's configuration."""
    path = configuration.CONFIG_PATH
    try:
        files = gh.pull_files(number)
        names = {f.get("filename") for f in files} | {f.get("previous_filename") for f in files}
        if path not in names:
            if len(files) < PULL_FILES_LIMIT:
                return ""
            # GitHub lists only the first files: compare the file with the merge base's.
            found = gh.compare(base, head)
            merge_base = (found.get("merge_base_commit") or {}).get("sha") or base
            if gh.file_text(path, merge_base) == gh.file_text(path, head):
                return ""
        text = gh.file_text(path, head)
    except GitHubError as exc:
        return f"NexKit could not read {path} at {head[:7]}: {exc}"
    if text is None:
        return f"{path} is missing at {head[:7]}, or larger than the 1 MB NexKit reads"
    try:
        configuration.validate(configuration.parse(text))
    except configuration.ConfigError as exc:
        return str(exc)
    return ""


def _decide(gh, command, number, on_pull, note, *, auto):
    if on_pull:
        if command not in PR_COMMANDS:
            return _none(f"`/nexkit {command}` is not a pull request command. {USAGE}", number)
        pull = gh.pull(number)
        if (pull["head"].get("repo") or {}).get("full_name") != gh.repository:
            return _none(
                "This pull request comes from a fork, so NexKit runs nothing on it: its checks "
                "would run code that nobody with write access pushed.",
                number,
            )
        issue = issue_from_branch(pull["head"]["ref"])
        # A person's pull request: NexKit checks and reviews it on request, and no more.
        person = issue is None
        if person and command != "review":
            return _none(
                f"`/nexkit {command}` acts only on NexKit's pull requests, and a person makes "
                "any change here. `/nexkit review` checks and reviews this one.",
                number,
            )
        if pull["state"] != "open":
            return _none("The pull request is closed", number)
        merged = on_base(gh, pull["base"]["ref"], pull["head"]["sha"])
        if merged and person:
            return _none(
                f"This pull request changes no file against `{pull['base']['ref']}`, so there "
                "is nothing to review.",
                number,
            )
        if merged:
            # Its base branch already holds its change: no round, review or merge would
            # change anything, so the `maintain` job closes it and its issue.
            return {
                "action": "maintain",
                "task": "on_base",
                "issue": issue,
                "pr": number,
                "base": pull["base"]["ref"],
                "head": pull["head"]["sha"],
                "merge_base": merged,
            }
        decision = {
            "action": PR_COMMANDS[command],
            # GitHub keeps a pull request's text and comments as an issue's.
            "issue": number if person else issue,
            "pr": number,
            "target": number,
            "branch": pull["head"]["ref"],
            "base": pull["base"]["ref"],
            "head": pull["head"]["sha"],
            "note": note,
            "auto": auto,
        }
        if person:
            decision["person"] = True
            error = head_config_error(gh, number, pull["base"]["ref"], pull["head"]["sha"])
            if error:
                # `report` fails `nexkit/checks` with it, so the change cannot merge.
                decision["config_error"] = error
        comments = gh.comments(number)
        # The last commit whose checks passed, for NEXKIT_VERIFIED_SHA.
        verified = verified_head(comments)
        if verified:
            decision["verified_sha"] = verified
        run_id = results_run(comments)
        if run_id and decision["action"] == "fix":
            # The agent job downloads that run's `nexkit-checks` artifact for the prompt.
            decision["results_run"] = run_id
        repeated = repeated_failures(comments)
        if repeated and auto and decision["action"] == "fix":
            # Checks that failed the same way before and after the last fix round: this
            # automatic round looks for their cause first.
            decision["diagnose"] = repeated
        return decision

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
    if not cfg["profiles"] or decision["action"] in ("none", "maintain"):
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


# The most files GitHub's comparison of two commits lists: one that lists as many may have
# left some out.
COMPARE_FILES = 300


def changed_paths(gh, since, head):
    """The paths that changed from `since` to `head`, both names of a renamed file, or
    None when GitHub cannot list them all: the comparison fails, `head` does not descend
    from `since` (a diff from their merge base would miss changes), or it lists as many
    files as it can."""
    try:
        found = gh.compare(since, head)
    except GitHubError:
        return None
    if not isinstance(found, dict) or found.get("status") not in ("ahead", "identical"):
        return None
    files = found.get("files")
    if not isinstance(files, list) or len(files) >= COMPARE_FILES:
        return None
    paths = set()
    for item in files:
        names = [item.get("filename")] if isinstance(item, dict) else [None]
        if isinstance(item, dict) and "previous_filename" in item:
            names.append(item["previous_filename"])
        if not all(isinstance(name, str) and name for name in names):
            return None
        paths.update(names)
    return paths


def skip_unaffected(gh, decision, cfg):
    """`decision` with `setup`, the round's setup as its marker records it; in a round of
    a pull request, `check_heads`: for each check and command, the head of the latest
    round on which it passed with this setup, for NEXKIT_CHECK_VERIFIED_SHA; and, in an
    automatic `review` round, `skipped_checks`: [{name, since}] for each check whose
    `unaffected_by` lists every path that changed since `since`, its head for its
    command. Those checks run on no runner and pass. Only there does `route` know the
    head the checks run on; a person's `/nexkit review` runs every check. Any doubt runs
    the check (see `changed_paths`)."""
    if decision["action"] not in ("implement", "fix", "review"):
        return decision
    setup = setup_fingerprint(cfg["setup"])
    decision = {**decision, "setup": setup}
    if not decision.get("pr"):
        return decision
    heads = check_heads(gh.comments(decision["pr"]), setup=setup)
    if heads:
        decision["check_heads"] = heads
    # A sharded check runs as shards, which the `shards` job skips by their own heads.
    listed = [
        check for check in cfg["checks"] if check.get("unaffected_by") and not check.get("shards")
    ]
    if decision["action"] != "review" or not decision.get("auto") or not listed:
        return decision
    changed, skipped = {}, []
    for check in listed:
        since = (heads.get(check["name"]) or {}).get(check_fingerprint(check["run"]))
        if not since:
            continue
        if since not in changed:
            changed[since] = changed_paths(gh, since, decision["head"])
        paths = changed[since]
        if paths is not None and unaffected(paths, check["unaffected_by"]):
            skipped.append({"name": check["name"], "since": since})
    if skipped:
        decision["skipped_checks"] = skipped
    return decision
