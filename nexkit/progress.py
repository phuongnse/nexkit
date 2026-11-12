"""Show each run in its own comment, posted where the run was started.

`route` calls `start` once a command is accepted, which posts the run comment; `report`
edits that comment with the outcome. Nothing in between can write to GitHub, because the
agent, verify and review jobs keep read-only tokens.
"""

from __future__ import annotations

import copy
import sys

from .checks import FAILURE_LINES, failing_lines
from .github import GitHubError, run_url
from .state import (
    CONTINUED,
    PAUSED,
    RUNNING,
    SUCCESS,
    TEXT_LIMIT,
    find_run,
    next_round,
    render_run,
    render_state,
    run_comments,
    with_run,
)

COMMANDS = {"plan": "plan", "implement": "go", "fix": "fix", "review": "review"}
STATUS_CONTEXTS = ("nexkit/checks", "nexkit/review")


def kinds(decision):
    """The kinds of a round, in order: `auto`, `diagnosis`, `conflicts`, `resumed`, `retry`,
    `autopilot` and `person`, for a person's pull request, each when it applies."""
    return [
        label
        for label, on in (
            ("auto", decision.get("auto")),
            ("diagnosis", decision.get("diagnose")),
            ("conflicts", decision.get("conflicts")),
            ("resumed", decision.get("resumed")),
            ("retry", decision.get("retry")),
            ("autopilot", decision.get("autopilot")),
            ("person", decision.get("person")),
        )
        if on
    ]


def shown_kinds(labels, retry):
    """`labels` as people read them: the `retry` kind with its number, as `retry 2`."""
    number = retry if isinstance(retry, int) and not isinstance(retry, bool) else 1
    return [f"retry {number}" if label == "retry" else label for label in labels]


def trigger(decision):
    """`fix`, `fix (auto)`, `fix (auto, diagnosis)`, `fix (auto, conflicts)`, `review
    (resumed)`, `go (retry 2)`, ...: for people. NexKit reads a round's `action` and `kinds`
    instead."""
    labels = shown_kinds(kinds(decision), decision.get("retry"))
    return decision["action"] + (f" ({', '.join(labels)})" if labels else "")


def _save(gh, number, comment_id, body):
    if comment_id:
        gh.update_comment(comment_id, body)
    else:
        gh.comment(number, body)


def find_round(comments, decision):
    """(comment_id, round) for this run's round comment, or (None, a new round)."""
    url = run_url()
    comment_id, row = find_run(comments, url)
    row = row or {"round": next_round(comments), "url": url}
    row["trigger"] = trigger(decision)
    row["action"], row["kinds"] = decision["action"], kinds(decision)
    # `setup`: which setup the round's checks ran, for the next rounds' skips (route).
    for key in ("conflicts", "resumed", "retry", "base_sha", "setup"):
        if decision.get(key):
            row[key] = decision[key]
    return comment_id, row


def save_round(gh, pr, comment_id, row, text=""):
    """Edit the round's comment, or post it when there is none (for example, deleted)."""
    _save(gh, pr, comment_id, render_run(row, text))


# What `fit_state` shortens, in this order, until the state comment fits: each step keeps
# the most it can down to its floor, and only when that is not enough goes on to the next.
# A failing check's output keeps its end, as NexKit stores it; a finding keeps its start.
FIT_STEPS = (
    ("output", 2_000),
    ("suggestions", 300),
    ("failure_lines", 0),
    ("output", 0),
    ("blocking", 300),
    ("suggestions", 0),
    ("blocking", 0),
    ("fixes", 0),
)


def _fit_failing(state):
    """The failing checks of the state's feedback that `fit_state` may shorten: those whose
    failure signature it recorded, so shortening never changes how they compare."""
    checks = (state.get("feedback") or {}).get("checks") or []
    return [
        check
        for check in checks
        if isinstance(check, dict) and not check.get("passed") and check.get("signature")
    ]


def _fit_findings(state, blocking):
    """The blocking, or other, findings of the state's feedback and of its last review."""
    found = []
    for review in ((state.get("feedback") or {}).get("review"), state.get("last_review")):
        findings = review.get("findings") if isinstance(review, dict) else None
        for finding in findings or []:
            if isinstance(finding, dict) and (finding.get("severity") == "blocking") == blocking:
                found.append(finding)
    return found


def _fit_most(state, key):
    """How much of what `key` caps the state holds, so no cap above it changes anything."""
    if key == "output":
        return max((len(c.get("output") or "") for c in _fit_failing(state)), default=0)
    if key == "failure_lines":
        return FAILURE_LINES
    if key == "fixes":
        return len(state.get("fixes") or [])
    found = _fit_findings(state, key == "blocking")
    return max((len(str(f.get("body") or "")) for f in found), default=0)


def _fitted(state, caps):
    """A copy of `state` with what `caps` names cut to its cap."""
    state = copy.deepcopy(state)
    for check in _fit_failing(state):
        output, most = check.get("output") or "", caps.get("output")
        if most is not None and len(output) > most:
            # Its end, as NexKit stores it: the summary that test runners print last. The
            # lines that report a failure in what goes join those kept from before it.
            cut = len(output) - most
            earlier = list(check.get("failure_lines") or []) + failing_lines(output[:cut])
            check["output"] = output[cut:]
            if earlier:
                check["failure_lines"] = earlier[:FAILURE_LINES]
            check["output_cut"] = True
        lines, most = check.get("failure_lines"), caps.get("failure_lines")
        if most is not None and lines and len(lines) > most:
            check["failure_lines"] = lines[:most]
            check["output_cut"] = True
    for key in ("suggestions", "blocking"):
        most = caps.get(key)
        if most is None:
            continue
        for finding in _fit_findings(state, key == "blocking"):
            body = str(finding.get("body") or "")
            if len(body) > most:
                finding["body"] = body[:most] + " […]"
    most = caps.get("fixes")
    if most is not None:
        state["fixes"] = (state.get("fixes") or [])[len(state.get("fixes") or []) - most :]
    return state


def fit_state(state, limit=TEXT_LIMIT):
    """`state` as its comment can hold it: unchanged when its comment fits in `limit`
    characters, else with what FIT_STEPS names shortened, each step to the most that fits,
    so that a pull request with many failing checks or a long review keeps its state. A
    shortened check's output says so (`output_cut`) to the prompt that shows it."""

    def fits(caps):
        return len(render_state(_fitted(state, caps))) <= limit

    caps = {}
    if fits(caps):
        return state
    for key, floor in FIT_STEPS:
        most = _fit_most(state, key)
        if caps.get(key) is not None:
            most = min(most, caps[key])
        if most <= floor:
            continue
        if fits({**caps, key: floor}):
            low, high = floor, most
            while low < high:
                middle = (low + high + 1) // 2
                low, high = (middle, high) if fits({**caps, key: middle}) else (low, middle - 1)
            return _fitted(state, {**caps, key: low})
        caps[key] = floor
    return _fitted(state, caps)


def save_state(gh, pr, comment_id, state):
    """Edit the state comment, within GitHub's limit on a comment's length (see
    `fit_state`). A v1.1.0 state comment is left as it is; a new one replaces it."""
    if "rounds" in state:
        state = {key: value for key, value in state.items() if key != "rounds"}
        state["version"] = 2
        comment_id = None
    _save(gh, pr, comment_id, render_state(fit_state(state)))


def show_command(gh, decision, status, text="", comments=None, **fields):
    """Post or edit the comment for this run of an issue command. `fields` go into its
    marker, such as the outcome."""
    issue = decision["issue"]
    url = run_url()
    comments = gh.comments(issue) if comments is None else comments
    comment_id, _ = find_run(comments, url)
    run = {"url": url, "command": COMMANDS[decision["action"]], "status": status, **fields}
    if decision.get("autopilot"):
        run["autopilot"] = True
    if decision.get("retry"):
        # Which run again after a transient error this is, shown with the command.
        run["retry"] = decision["retry"]
    if decision.get("setup"):
        # Which setup an implement round's checks ran, for a round stopped in its checks.
        run["setup"] = decision["setup"]
    _save(gh, issue, comment_id, render_run(run, text))


def replace_paused(gh, comments, why="Replaced by a later command.", recorded=True):
    """Stop paused runs in `comments` from resuming and from showing ⏸️: a new command
    carries on from them. With `recorded` false, only runs without a resume record."""
    for comment, run in run_comments(comments):
        if run.get("status") != PAUSED or (not recorded and run.get("resume")):
            continue
        run.pop("resume", None)
        run["status"] = CONTINUED
        gh.update_comment(comment["id"], with_run(comment["body"], run, why))


def start(gh, decision):
    """Acknowledge an accepted command and link the run. Never stops the run."""
    if decision.get("comment_id"):
        gh.react(decision["comment_id"], "eyes")
    try:
        number = decision.get("pr") or decision["issue"]
        comments = gh.comments(number)
        replace_paused(gh, comments)
        if decision.get("pr"):
            # `/nexkit go` shows a pause of its pull request's review on the issue too; the
            # pull request's record resumes it. Paused issue commands keep their own record.
            why = f"Continued in pull request #{decision['pr']}."
            replace_paused(gh, gh.comments(decision["issue"]), why, recorded=False)
            description = f"NexKit {decision['action']} round in progress"
            for context in STATUS_CONTEXTS:
                gh.set_status(decision["head"], context, "pending", description, run_url())
            comment_id, row = find_round(comments, decision)
            if row.get("head") != decision["head"]:
                # What passed on another commit is no record of this one.
                row.pop("passed", None)
            row.update(
                status=RUNNING, head=decision["head"], checks=None, verdict=None, tokens=None
            )
            save_round(gh, decision["pr"], comment_id, row)
        else:
            show_command(gh, decision, RUNNING, comments=comments)
    except GitHubError as exc:
        print(f"warning: could not show the run's progress: {exc}", file=sys.stderr)


def finish(gh, decision, status, text, **fields):
    """Show the outcome of an issue command and react to the command comment. A paused
    run, or one that runs once more, keeps 👀: it has not finished."""
    if not decision.get("pr"):
        show_command(gh, decision, status, text, **fields)
    if decision.get("comment_id") and status not in (PAUSED, CONTINUED):
        gh.react(decision["comment_id"], "rocket" if status == SUCCESS else "confused")
