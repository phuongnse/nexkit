"""Report a finished pipeline run on GitHub and decide what happens next."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import autopilot, progress
from .checks import failing_signatures
from .github import GitHubError, dispatched_runs_url, run_url
from .maintain import (
    ON_BASE_FAILED,
    close_on_base,
    close_parents_safely,
    is_conflicting,
    resolve_conflicts,
    start_conflict_round,
    start_waiting,
)
from .route import BRANCH_PREFIX, auto_input, on_base
from .runlog import add_tokens, one_line
from .schema import filled
from .state import (
    CONTINUED,
    FAILURE,
    PAUSED,
    SUCCESS,
    TROUBLESHOOTING,
    UNCOUNTED,
    clip,
    empty_state,
    options_of,
    passed_lines,
    questions,
    read_state,
    recommended_option,
    round_questions,
    run_comments,
    with_run,
)
from .usage import iso, parse_iso, shown

ICONS = {True: "✅", False: "❌"}
CRITERION_MARKS = {"met": "✅", "unmet": "❌", "unverifiable": "❔"}
# 🛑 marks only blocking findings, so an open suggestion keeps its own mark.
RESOLUTIONS = {
    "resolved": "✅ resolved",
    "rejection_accepted": "🤝 rejection accepted",
}
OPEN = {"blocking": "🛑 Blocking, unresolved", "suggestion": "💡 Suggestion, still open"}
# Outcomes in which the run did its work. The others stopped on an error.
COMPLETED = {
    "planned",
    "ready",
    "merged",
    "merge_refused",
    "auto_fix",
    "needs_human",
    "up_to_date",
    "conflict_fix",
    "merge_waiting",
    "branch_updated",
    "head_moved",
    "recheck_failed",
    "no_change",
    "already_on_base",
    "issue_closed",
    "merge_issue_closed",
    autopilot.CI_WAITING,
}
# Why a person is needed after a run with this outcome. The other outcomes need nobody:
# NexKit continues by itself (`auto_fix`, `conflict_fix`, a resumed pause, a sweep after
# `ci_waiting`) or is done.
NEEDS_PERSON = {
    "agent_blocked": "the agent is blocked and needs a decision",
    "agent_error": "the run stopped on an error",
    "agent_failed": "the agent could not run",
    "publish_failed": "NexKit could not publish the result",
    "review_failed": "the AI review did not complete",
    "needs_human": "checks or review still need attention and no automatic fix rounds remain",
    "merge_refused": "GitHub refused the automatic merge",
    "merge_issue_closed": (
        "the issue closed before NexKit merged its pull request; merge or close it yourself"
    ),
    "retry_failed": "the run stopped on a transient error twice",
    "close_failed": "no change is needed, but NexKit could not close the issue; close it yourself",
    "recheck_failed": (
        "the checks or review still fail after the fix agent found nothing to change and "
        "NexKit ran them again"
    ),
    "needs_merge": (
        "the base branch changed workflow files that NexKit may not push; merge it into the "
        "pull request yourself"
    ),
    "on_base_failed": ON_BASE_FAILED,
}
NO_CHANGE_PLAN = "the plan finds that no change is needed; a person closes the issue"
UNVERIFIED = "the review could not verify every acceptance criterion; confirm them before merging"
# Bounds for what the state comment keeps for the next review.
MAX_FINDINGS = 30
MAX_FIXES = 5
# How a failed review's reason fits in a commit status; GitHub allows 140 characters.
STATUS_LIMIT = 140
# An agent's error that another try cannot fix: the credential, endpoint or deployment.
ACCESS_ERROR = re.compile(
    r"\b(?:401|403)\b|unauthori[sz]ed|forbidden|invalid (?:api|subscription) key"
    r"|authentication",
    re.IGNORECASE,
)


def _load(path):
    path = Path(path)
    return json.loads(path.read_text()) if path.is_file() else None


def _tokens(*results):
    """The tokens of the stages that ran, or None when one of them has no count."""
    return add_tokens(*(r.get("tokens") for r in results if r))


def agents_ran(*results):
    """Which agents ran each stage of `results`, for the run comment: the agents that ran
    in turn, without those skipped, with the model each ran, and whether the stage paused
    at their limits."""
    entries = []
    for result in results:
        for item in (result or {}).get("triage"), result:
            if not item or not item.get("attempts"):
                continue
            ran = [a for a in item["attempts"] if a.get("status") != "skipped"]
            entries.append(
                {
                    "stage": item["stage"],
                    "agents": [a["agent"] for a in ran],
                    "models": [a.get("model") for a in ran],
                    "paused": item["status"] == "paused",
                }
            )
    return entries


def _show_agents(row, *results):
    """Name the agents in a round's comment, once some agent ran."""
    entries = agents_ran(*results)
    if entries:
        row["agents"] = entries


def _location(item):
    if not item.get("file"):
        return ""
    line = f":{item['line']}" if item.get("line") else ""
    return f" (`{item['file']}{line}`)"


def _part(title, lines):
    return ["", f"**{title}**", "", *lines] if lines else []


def criterion_status(item):
    """`met`, `unmet` or `unverifiable`. Reviews stored before criteria had a status have a
    boolean `met`."""
    if "status" in item:
        return item["status"]
    return "met" if item.get("met") else "unmet"


def _criteria(review, status):
    return [c for c in (review or {}).get("criteria") or [] if criterion_status(c) == status]


def apply_criteria(review):
    """The review NexKit acts on. Each unmet criterion is a blocking finding and requests
    changes, whatever verdict the reviewer returned."""
    unmet = _criteria(review, "unmet")
    if not unmet:
        return review
    findings = []
    for c in unmet:
        body = f"Acceptance criterion not met: {c['criterion']}. {c.get('evidence') or ''}"
        findings.append({"severity": "blocking", "file": "", "line": 0, "body": body.strip()})
    return {
        **review,
        "verdict": "request_changes",
        "findings": [*(review.get("findings") or []), *findings],
    }


def unverified(review):
    """The criteria of an approving review that a person must confirm before a merge."""
    if not review or review["verdict"] != "approve":
        return []
    return [c["criterion"] for c in _criteria(review, "unverifiable")]


def review_body(review, checks):
    """What a person acts on comes first: verdict, findings, criteria. Evidence is collapsed."""
    given = review["output"]
    output = apply_criteria(given)
    approved = output["verdict"] == "approve"
    findings = []
    for item in sorted(output.get("findings") or [], key=lambda f: f["severity"] != "blocking"):
        mark = "🛑 Blocking" if item["severity"] == "blocking" else "💡 Suggestion"
        findings.append(f"- {mark}: {item['body'].strip()}{_location(item)}")
    previous, previous_evidence = [], []
    for item in output.get("previous_findings") or []:
        if item["resolution"] == "unresolved":
            mark = OPEN.get(item.get("severity"), "unresolved")
        else:
            mark = RESOLUTIONS.get(item["resolution"], item["resolution"])
        previous.append(f"- {mark}: {item['finding']}")
        if item.get("evidence"):
            previous_evidence.append(f"- {mark}: {item['finding']}: {item['evidence'].strip()}")
    criteria, criteria_evidence = [], []
    for item in output.get("criteria") or []:
        mark = CRITERION_MARKS[criterion_status(item)]
        test = f"`{item['test']}`" if item.get("test") else "no test"
        criteria.append(f"- {mark} {item['criterion']} ({test})")
        if item.get("evidence"):
            criteria_evidence.append(f"- {mark} {item['criterion']}: {item['evidence'].strip()}")

    lines = [
        f"### NexKit review: {'approve' if approved else 'changes requested'}",
        "",
        output["summary"].strip(),
    ]
    if given["verdict"] == "approve" and not approved:
        lines += ["", "NexKit requests changes because an acceptance criterion is not met."]
    confirm = unverified(output)
    if confirm:
        lines += [
            "",
            "The review could not verify every acceptance criterion (❔). A person confirms "
            "them before the merge.",
        ]
    lines += _part("Things to look at", findings)
    lines += _part("Previous findings", previous)
    lines += _part("Acceptance criteria", criteria)
    if checks:
        lines += [
            "",
            "**Checks**: " + ", ".join(f"{ICONS[c['passed']]} `{c['name']}`" for c in checks),
        ]
    evidence = _part("Acceptance criteria", criteria_evidence)
    evidence += _part("Previous findings", previous_evidence)
    if evidence:
        # GitHub renders Markdown inside <details> only after a blank line.
        lines += ["", "<details><summary>Evidence</summary>", *evidence, "", "</details>"]
    return "\n".join(lines)


def workflow_file(workflow_ref):
    """`owner/repo/.github/workflows/nexkit.yml@refs/heads/main` -> `nexkit.yml`."""
    return workflow_ref.split("@", 1)[0].rsplit("/", 1)[-1]


def _set_statuses(gh, head, checks, review, url, review_error=""):
    """Set both commit statuses from check results and a review output (None: not run).
    `review_error` says why a review that did not complete stopped."""
    checks_ok = checks is not None and all(c["passed"] for c in checks)
    gh.set_status(
        head,
        "nexkit/checks",
        "success" if checks_ok else "failure",
        "Checks passed"
        if checks_ok
        else ("Checks did not run" if checks is None else "Some checks failed"),
        url,
    )
    verdict = review["verdict"] if review else None
    if verdict:
        if verdict != "approve":
            text = "AI review requested changes"
        elif review.get("carried"):
            text = f"AI review of {review['carried'][:7]} carried over; the change is the same"
        elif unverified(review):
            text = "AI review approved; a person confirms the criteria it could not verify"
        else:
            text = "AI review approved"
        gh.set_status(
            head, "nexkit/review", "success" if verdict == "approve" else "failure", text, url
        )
    else:
        text = "AI review did not complete"
        if review_error:
            text = f"{text}: {review_error}"
        if len(text) > STATUS_LIMIT:
            text = text[: STATUS_LIMIT - 1] + "…"
        gh.set_status(head, "nexkit/review", "error", text, url)
    return checks_ok, verdict


def review_failure(reviewed):
    """(reason, message) for a round whose review gave no verdict: why it stopped, from the
    review's result, and what a person can do about it."""
    if not reviewed:
        reason = "the review job left no result"
    elif reviewed.get("error"):
        reason = one_line(reviewed["error"])[:500]
    else:
        reason = "the review agent returned no verdict"
    if reviewed and ACCESS_ERROR.search(reason):
        advice = (
            "Another try will stop the same way until the review agent's secrets are fixed: "
            f"see [Troubleshooting]({TROUBLESHOOTING}). Then comment `/nexkit review`."
        )
    else:
        advice = "Comment `/nexkit review` to try again."
    return reason, f"The AI review did not complete: {reason}\n\n{advice}"


def _restore_statuses(gh, sha, feedback, url):
    """Replace the pending statuses `route` set on a commit this run did not check."""
    feedback = feedback or {}
    if feedback.get("head") == sha:
        _set_statuses(gh, sha, feedback.get("checks"), feedback.get("review"), url)
        return
    for context in progress.STATUS_CONTEXTS:
        gh.set_status(sha, context, "error", "The NexKit round did not finish", url)


def _text(item, key):
    return str(item.get(key) or "").strip()


def open_conflicts(agent, base):
    """The conflicts with the base branch that a blocked fix round left for a person."""
    items = [i for i in (agent.get("output") or {}).get("open_conflicts") or [] if i]
    if agent.get("status") != "blocked" or not items:
        return ""
    lines = [f"**Conflicts with `{base}` that need your decision**", ""]
    for item in items:
        lines += [
            f"- `{_text(item, 'file')}`",
            f"  - `{base}`: {_text(item, 'base_change')}",
            f"  - This pull request: {_text(item, 'pr_change')}",
            f"  - **Question:** {_text(item, 'question')}",
            *_options(item),
        ]
    lines += [
        "",
        "Answer in one comment: `/nexkit fix` followed by your decisions, for example "
        f"`/nexkit fix Keep the check from {base} and add this pull request's endpoint after "
        f"it.` The next round merges `{base}` again, follows your note and resolves the "
        "other conflicts itself.",
    ]
    return "\n".join(lines)


def _options(item, indent="    "):
    recommended = recommended_option(item)
    return [
        f"{indent}- {option}" + (" (recommended)" if option == recommended else "")
        for option in options_of(item)
    ]


def blocked_questions(agent):
    """The decisions a blocked round asks of a person, other than conflicts."""
    items = questions((agent.get("output") or {}).get("questions"))
    if agent.get("status") != "blocked" or not items:
        return ""
    lines = ["**Decisions needed**", ""]
    for item in items:
        lines += [f"- {item['question']}", *_options(item, "  ")]
    return "\n".join(lines)


def _autopilot_fields(agent):
    """What autopilot reads from a run that stopped: the questions of a blocked round,
    whether the agent ran out of time, and the protected paths its change needs, which
    only a person changes."""
    fields = {}
    if agent and agent.get("timed_out"):
        fields["timed_out"] = True
    found = round_questions(agent)
    if found:
        fields["questions"] = found
    protected = protected_paths(agent)
    if protected and agent.get("status") == "blocked":
        fields["protected_paths"] = protected
    finding = no_change_finding(agent)
    if finding and agent.get("stage") == "implement":
        # The round recommends closing the issue with this finding.
        fields["no_change"] = finding
    return fields


def protected_paths(agent):
    """The protected paths a plan or round says its change needs, as the agent job kept
    them: only paths that are protected."""
    found = ((agent or {}).get("output") or {}).get("protected_paths") or []
    return [clip(path, 300) for path in found if isinstance(path, str)][:20]


def no_change_finding(agent):
    """What an agent found when it says no change is needed, with the runs that passed on
    the default branch since the CI runs the issue names, or ""."""
    finding = filled(((agent or {}).get("output") or {}).get("no_change"))
    if not finding:
        return ""
    passed = passed_lines((agent or {}).get("passed_since"))
    return clip("\n\n".join([finding, *(["\n".join(passed)] if passed else [])]), 3500)


def _no_change(gh, cfg, decision, agent):
    """An implement round that found no change is needed, where the issue or its plan says
    to stop then: close the issue as completed, with the agent's finding."""
    issue = decision["issue"]
    finding = no_change_finding(agent)
    try:
        gh.close_issue(issue, "completed")
    except GitHubError as exc:
        message = f"No change is needed, but NexKit could not close #{issue}: {exc}"
        return {
            "outcome": "close_failed",
            "summary": f"{message}\n\n{finding}",
            "attention": needs_person("close_failed", cfg),
            "agents": agents_ran(agent),
        }
    lines = [f"No change is needed, so NexKit closed #{issue} as completed.", "", finding]
    if cfg["close_parent_issues"]:
        # Closing with the Actions token starts no workflow for `issues: closed`.
        lines += ["", *close_parents_safely(gh, issue)]
    return {
        "outcome": "no_change",
        "summary": "\n".join(lines).strip(),
        "agents": agents_ran(agent),
    }


def _already_on_base(gh, cfg, decision, agent):
    """A fix round that found, before its agent, that the base branch already holds the
    pull request's change: close it and its issue (see `maintain.close_on_base`)."""
    target = {**decision, "merge_base": agent.get("merge_base") or decision["base"]}
    outcome, text, commit = close_on_base(gh, cfg, target, agent.get("on_base"))
    reason = needs_person(outcome, cfg)
    fields = {"on_base": commit} if commit else {}
    _stop_round(gh, decision, agent, outcome, text, reason, **fields)
    return {"outcome": outcome, "summary": text, "attention": reason}


def _stage_failure(decision, agent, publish):
    """(outcome, message) when the agent or publish stage stopped, otherwise None."""
    action = decision["action"]
    if agent is None:
        return "agent_failed", f"NexKit could not run the {action} agent."
    if agent["status"] == "needs_merge":
        return "needs_merge", f"NexKit did not run the {action} agent: {agent['error']}"
    if publish.get("issue_closed"):
        return "issue_closed", (
            f"#{decision['issue']} closed while the {action} round ran, so NexKit published "
            "nothing."
        )
    if agent["status"] != "done":
        message = f"NexKit stopped during {action}: {agent['error']}"
        extra = [blocked_questions(agent), open_conflicts(agent, decision["base"])]
        return "agent_" + agent["status"], "\n\n".join([message, *filter(None, extra)])
    if not publish.get("published") and not publish.get("recheck") and not publish.get("no_change"):
        reason = publish.get("error") or "the publish job failed"
        outcome = "needs_merge" if publish.get("workflows") else "publish_failed"
        return outcome, f"NexKit could not publish the {action} result: {reason}"
    return None


def _with_finding(reason, agent):
    """`reason` with what the fix agent found when it changed nothing, so that a person
    sees it in the mention and in `nexkit status`."""
    output = (agent or {}).get("output") or {}
    found = str(output.get("cause") or output.get("summary") or "").strip()
    if not reason or not found:
        return reason
    return f"{reason}; the agent found: {clip(one_line(found), 300)}"


def needs_person(outcome, cfg, plan=None, resume=None, confirm=()):
    """The reason a person is needed after a run, or None. It follows only the outcome and
    the run's structured output, never a judgment by the agent. `confirm` holds the
    criteria a ready round's review could not verify."""
    if outcome == "planned":
        plan = plan or {}
        if plan.get("too_large"):
            return "the plan says the issue is too large for one session"
        if filled(plan.get("no_change")):
            return NO_CHANGE_PLAN
        if plan.get("protected_paths"):
            return autopilot.protected_reason(plan["protected_paths"])
        return "the plan has questions" if plan.get("questions") else None
    if outcome == "merge_waiting":
        # Only a round whose issue autopilot's sweeps look at waits like this.
        return None
    if outcome == "ready":
        if confirm:
            return UNVERIFIED
        return None if cfg["merge"]["auto"] else "the pull request is ready for a human decision"
    if outcome == "paused":
        return (
            None
            if resume
            else "The agent hit its usage limit; run the command again after the reset"
        )
    return NEEDS_PERSON.get(outcome)


def notify(gh, cfg, number, reason):
    """Mention the people in `notify` once, in a new comment: GitHub notifies people
    mentioned in a new comment, not in an edited one."""
    text = autopilot.mention_text(cfg, reason) if reason else None
    if not text:
        return
    try:
        gh.comment(number, text)
    except GitHubError as exc:
        print(f"warning: could not mention {', '.join(cfg['notify'])}: {exc}", file=sys.stderr)


def run_status(outcome):
    if outcome == "paused":
        return PAUSED
    if outcome == "retry":
        # The command runs once more in a run of its own.
        return CONTINUED
    return SUCCESS if outcome in COMPLETED else FAILURE


def _transient(decision, agent, publish, needs, stage="agent"):
    """The error of a stage that stopped for a reason another try may pass, or None: a
    transient error of the agent, a job that failed as a whole, or, in a round NexKit
    started itself, a branch that moved while the agent worked. A job a person cancelled
    is never one."""
    result = (needs.get(stage) or {}).get("result")
    if result == "cancelled":
        return None
    if result == "skipped":
        # The review runs only after the checks: when their job failed as a whole, that
        # job is what another try may pass.
        for job in ("verify", "combine"):
            if (needs.get(job) or {}).get("result") == "failure":
                return f"the {job} job ended without a result"
        return None
    if agent is None:
        return f"the {stage} job ended without a result"
    if agent.get("status") == "error" and agent.get("transient"):
        return agent.get("error") or "a transient error"
    if stage == "agent" and publish.get("moved") and _automatic(decision):
        return publish.get("error")
    return None


def _automatic(decision):
    return bool(decision.get("auto") or decision.get("autopilot"))


def _retry(gh, decision, command, number, workflow_ref, default_branch):
    """Start `command` once more after a transient error, with the same note for the same
    command. Returns whether it started: never for a run that was itself the retry."""
    if decision.get("retry"):
        return False
    same = command == progress.COMMANDS[decision["action"]]
    note = (decision.get("note") or "") if same else ""
    if same:
        auto = auto_input(decision, retry=True)
    else:
        auto = auto_input({"autopilot": decision.get("autopilot")}, retry=True)
    inputs = {"command": command, "number": str(number), "note": note, "auto": auto}
    try:
        gh.dispatch(workflow_file(workflow_ref), default_branch, inputs)
    except GitHubError as exc:
        print(f"warning: could not start the command once more: {exc}", file=sys.stderr)
        return False
    return True


def _earlier_error(gh, decision):
    """The transient error that the run before this retry stopped on, or "" ."""
    number = decision.get("pr") or decision["issue"]
    for _, run in reversed(list(run_comments(gh.comments(number)))):
        if run.get("transient_error") and run.get("url") != run_url():
            return run["transient_error"]
    return ""


RETRY_LINE = (
    "The error looks transient, from the service, the network or the runner rather than the "
    "work, so NexKit runs the command once more."
)


def _retried(gh, decision, error, message):
    """The message of a retry that stopped on a transient error again, with both errors."""
    earlier = _earlier_error(gh, decision)
    if earlier:
        message += f"\n\nThe try before stopped on a transient error too: {earlier}"
    return message


def _stop_round(gh, decision, agent, outcome, message, reason=None, **fields):
    """Show a pull request round that published nothing, with the reason. An automatic
    fix round that stopped for a reason outside the agent's work gives its round back."""
    comments = gh.comments(decision["pr"])
    state_id, state = read_state(comments)
    _restore_statuses(gh, decision["head"], (state or {}).get("feedback"), run_url())
    counted = decision.get("auto") and not decision.get("conflicts")
    if outcome in UNCOUNTED and counted and state and state.get("auto_fixes"):
        state["auto_fixes"] -= 1
        progress.save_state(gh, decision["pr"], state_id, state)
    comment_id, row = progress.find_round(comments, decision)
    status = run_status(outcome)
    row.update(status=status, head=decision["head"], tokens=_tokens(agent), outcome=outcome)
    _show_agents(row, agent)
    row.update(_autopilot_fields(agent))
    row.update(fields)
    if reason:
        row["attention"] = reason
    progress.save_round(gh, decision["pr"], comment_id, row, message)


def pause(result, cfg, command, number, note="", auto="resume", resumed=False):
    """(message, resume record or None) for a stage that stopped at the usage limit.

    The resume record goes into the run comment's marker; `maintain` starts the command
    again once its time has passed."""
    at = result.get("resume_at")
    known = result.get("reset_known")
    shown_command = f"`/nexkit {command}`" + (" with the same note" if note else "")
    # A plan that paused in triage has the attempts of its triage.
    attempts = result.get("attempts") or (result.get("triage") or {}).get("attempts") or []
    limited = [a for a in attempts if a.get("status") == "paused"]
    who = "Every agent stopped at its" if len(limited) > 1 else "The agent stopped at the account's"
    message = f"⏸️ {who} usage limit: {result.get('limit') or ''}"
    message = message.rstrip(": ") + "."
    if resumed and not known:
        # A run that was resumed blind and stopped again: a person decides, so a limit
        # that never resets cannot start a run every hour.
        at = None
    if not cfg["usage_limit"]["resume"] or not at:
        reset = f"It resets at {shown(at)}." if known and at else "The reset time is unknown."
        return f"{message} {reset} Comment {shown_command} after the reset to continue.", None
    if known:
        message += f" NexKit runs {shown_command} again after it resets, at {shown(at)}."
    else:
        message += (
            f" NexKit could not read when it resets, so it runs {shown_command} again after "
            f"{shown(at)}."
        )
    message += " A new command here replaces this."
    resume = {"command": command, "number": number, "note": note or "", "auto": auto, "at": at}
    return message, resume


def _pause_statuses(gh, head, resume, url):
    """Keep both statuses pending while the round waits for the usage limit to reset."""
    description = "Paused at the agent's usage limit" + (
        f"; resumes after {shown(resume['at'])}" if resume else ""
    )
    for context in progress.STATUS_CONTEXTS:
        gh.set_status(head, context, "pending", description, url)


def _paused_agent(gh, decision, cfg, agent):
    """The outcome of a plan, implement or fix stage that stopped at the usage limit."""
    command = progress.COMMANDS[decision["action"]]
    message, resume = pause(
        agent,
        cfg,
        command,
        decision["target"],
        decision.get("note"),
        auto_input(decision, resume=True),
        resumed=bool(decision.get("resumed")),
    )
    reason = needs_person("paused", cfg, resume=resume)
    at = resume and resume["at"]
    if not decision.get("pr"):
        return {
            "outcome": "paused",
            "summary": message,
            "resume": resume,
            "resume_at": at,
            "attention": reason,
            "agents": agents_ran(agent),
        }
    _pause_statuses(gh, decision["head"], resume, run_url())
    comment_id, row = progress.find_round(gh.comments(decision["pr"]), decision)
    row.update(status=PAUSED, head=decision["head"], tokens=_tokens(agent), outcome="paused")
    _show_agents(row, agent)
    row.update({key: value for key, value in (("resume", resume), ("attention", reason)) if value})
    progress.save_round(gh, decision["pr"], comment_id, row, message)
    return {"outcome": "paused", "summary": message, "resume_at": at, "attention": reason}


def after_merge(gh, workflows, base, records=None):
    """Start each workflow on the base branch after NexKit merged into it.

    GitHub starts no workflow for the merge's push, which uses the Actions token, but it does
    for a dispatch. A dispatch that fails is reported; the merge stays. Each start is added
    to `records`, for the round's marker.
    """
    lines = []
    records = [] if records is None else records
    for workflow in workflows:
        try:
            run = gh.dispatch(workflow, base, {}, run_details=True) or {}
            url = run.get("html_url") or dispatched_runs_url(workflow, base)
            lines.append(f"- Started [`{workflow}`]({url})")
            records.append({"workflow": workflow, "run_id": run.get("workflow_run_id"), "url": url})
        except GitHubError as exc:
            lines.append(f"- Could not start `{workflow}`: {exc}")
            records.append({"workflow": workflow, "error": str(exc)[:300]})
    return "\n".join([f"After the merge, on `{base}`:", *lines]) if lines else ""


# Why GitHub refused a merge, for refusals that pass by themselves or by NexKit's rounds:
# another merge into the base branch at the same moment, required checks that have not
# reported yet, and a branch that branch protection wants up to date with its base.
BASE_MODIFIED = re.compile(r"base branch was modified", re.I)
# A push to the pull request's branch after the round checked it, such as a person's.
HEAD_MODIFIED = re.compile(r"head branch was modified", re.I)
CHECKS_PENDING = re.compile(
    r"required status checks? .*\b(?:is|are) (?:expected|pending|in progress|queued)", re.I
)
BEHIND = re.compile(r"out of date|not up to date|is behind", re.I)
# Required checks that failed, so GitHub refused the merge, and the checks named.
CHECKS_FAILING = re.compile(r"required status checks?\b.*\b(?:failing|failed)\b", re.I)
CHECK_NAME = re.compile(r'"([^"]+)"')
# How often a merge refused because the base branch was modified is tried, and the seconds
# before the first try again; each later try waits that much longer.
MERGE_TRIES = 3
MERGE_DELAY = 10
# How long, and how often, a merge after updating the branch waits for GitHub to move it.
UPDATE_WAIT = 5
UPDATE_TRIES = 6


def merge_pull(gh, cfg, target, row, message, workflow, default_branch):
    """Merge a pull request that checks and review approved: `target` names its `pr`,
    `head`, `issue` and `base`. Returns (outcome, message), `message` being the round's
    text so far. A merge refused because the base branch was modified meanwhile is tried
    again a few times; other refusals that pass are handed on (see `_refused`). A pull
    request whose base branch already holds its change is closed instead, with its issue
    (see `maintain.close_on_base`)."""
    pr, head = target["pr"], target["head"]
    try:
        closed = gh.issue(target["issue"]).get("state") != "open"
    except GitHubError as exc:
        message += f" NexKit did not merge it: it could not read #{target['issue']}: {exc}"
        return "merge_refused", message.strip()
    if closed:
        # A person closed the issue while the pull request waited: they decide its fate.
        message += (
            f" NexKit did not merge it: #{target['issue']} is closed. Merge the pull request "
            "or close it yourself."
        )
        return "merge_issue_closed", message.strip()
    merge_base = on_base(gh, target["base"], head)
    if merge_base:
        outcome, text, commit = close_on_base(gh, cfg, {**target, "merge_base": merge_base})
        if commit:
            row["on_base"] = commit
        return outcome, f"✅ Checks and AI review passed on `{head[:7]}`.\n\n{text}"
    for attempt in range(MERGE_TRIES):
        try:
            merge = gh.merge(pr, head) or {}
            break
        except GitHubError as exc:
            if attempt + 1 < MERGE_TRIES and BASE_MODIFIED.search(str(exc)):
                time.sleep(MERGE_DELAY * (attempt + 1))
                continue
            return _refused(gh, cfg, target, row, message, exc, workflow, default_branch)
    message = f"✅ Checks and AI review passed; merged `{head[:7]}`."
    if attempt:
        message += f" GitHub took it at try {attempt + 1}, after the base branch moved."
    closed, lines = close_merged_issue(gh, target["issue"], pr, merge.get("sha"))
    if closed and cfg["close_parent_issues"]:
        # Closing with the Actions token starts no workflow for `issues: closed`.
        lines += close_parents_safely(gh, target["issue"])
    lines += delete_merged_branch(gh, pr, target["base"])
    runs = []
    started = after_merge(gh, cfg["merge"]["after_workflows"], target["base"], runs)
    if runs:
        row["after_merge"] = runs
    if cfg["conflicts"]["auto_resolve"]:
        # The merge's push starts no workflow, so look for new conflicts here.
        swept = resolve_conflicts(
            gh, target["base"], workflow, default_branch, cfg["conflicts"]["max_rounds"], {pr}
        )
        started = "\n".join(filter(None, [started, *swept]))
    parts = ["\n".join(lines), started]
    return "merged", message + "".join(f"\n\n{part}" for part in parts if part)


def _refused(gh, cfg, target, row, message, exc, workflow, default_branch):
    """(outcome, message) after GitHub refused the merge: a conflict round for a conflict,
    `merge_waiting` for required checks that have not passed yet, which autopilot's next
    sweeps merge, an automatic fix round for required checks that failed, at the first try
    or while the merge waits (see `_required_failed`), an update of a branch that is
    behind with a review of the new commit, also when GitHub only says its required checks
    are expected, and otherwise `merge_refused` for a person."""
    pr, base = target["pr"], target["base"]
    message += f" Automatic merge was not possible: {exc}"
    auto_resolve, limit = cfg["conflicts"]["auto_resolve"], cfg["conflicts"]["max_rounds"]
    if auto_resolve and is_conflicting(gh, pr):
        started = start_conflict_round(gh, pr, base, workflow, default_branch, limit, row=row)
        if started.startswith("Started"):
            return "conflict_fix", f"{message}\n\nIt conflicts with `{base}`. {started}"
        return "merge_refused", f"{message}\n\n{started}" if started else message
    if CHECKS_FAILING.search(str(exc)):
        return _required_failed(gh, cfg, target, row, message, exc, workflow, default_branch)
    if CHECKS_PENDING.search(str(exc)):
        if auto_resolve and _behind(gh, pr):
            # Checks that passed on a head behind its base count as "expected" when branch
            # protection wants it up to date: they never pass without an update.
            return _update_branch(gh, target, row, message, limit, workflow, default_branch)
        if not _swept(gh, cfg, target["issue"]):
            line = "Required checks have not passed yet. Merge it once they pass."
            return "merge_refused", f"{message}\n\n{line}"
        row.setdefault("waiting_since", iso(datetime.now(UTC)))
        line = (
            "Required checks have not passed yet. The next autopilot sweep merges it once "
            "they pass."
        )
        return "merge_waiting", f"{message}\n\n{line}"
    if auto_resolve and BEHIND.search(str(exc)):
        return _update_branch(gh, target, row, message, limit, workflow, default_branch)
    if HEAD_MODIFIED.search(str(exc)):
        return _head_moved(gh, target, message, workflow, default_branch)
    return "merge_refused", message


def _required_failed(gh, cfg, target, row, message, exc, workflow, default_branch):
    """(outcome, message) after required checks failed, at the round's first try of the
    merge or while it waited for them: an automatic fix round within
    `fix.max_auto_rounds`, as after failing checks of a round, unless they fail as the
    default branch does while autopilot's bug issue about that is open (`ci_waiting`, see
    `autopilot.ci_wait`). The state comment keeps the failed checks for the fix round,
    which merges a base branch that has moved first.

    At the first try, `target["state"]` is the state that the reporting round saves, and
    the round starts the fix round for the `auto_fix` outcome; here it is only updated."""
    pr, head = target["pr"], target["head"]
    failed = autopilot.failed_checks(gh, head, CHECK_NAME.findall(str(exc)))
    reporting = target.get("state")
    if reporting is None:
        comment_id, state = read_state(gh.comments(pr))
        state = state or empty_state(target["issue"])
    else:
        comment_id, state = None, reporting

    def save():
        if reporting is None:
            progress.save_state(gh, pr, comment_id, state)

    state["feedback"] = {**(state.get("feedback") or {"head": head}), "required": failed}
    what = _failed_line(failed, head, bool(target.get("waiting")))
    limit = cfg["fix"]["max_auto_rounds"]
    if state["auto_fixes"] >= limit:
        save()
        line = (
            "No automatic fix rounds remain. Comment `/nexkit fix <instructions>`, push a "
            "commit yourself, or close the PR."
        )
        return "needs_human", f"{message}\n\n{what} {line}"
    decision = {"base": target["base"], "issue": target["issue"]}
    wait = _ci_wait(gh, cfg, decision, autopilot.as_checks(failed), default_branch)
    if wait:
        state["ci_wait"] = row["ci_wait"] = wait
        save()
        return autopilot.CI_WAITING, f"{message}\n\n{what} {autopilot.ci_wait_message(wait)}"
    if reporting is None:
        inputs = {"command": "fix", "number": str(pr), "note": "", "auto": "true"}
        gh.dispatch(workflow, default_branch, inputs)
    state["auto_fixes"] += 1
    save()
    line = f"Starting automatic fix round {state['auto_fixes']} of {limit}."
    return "auto_fix", f"{message}\n\n{what} {line}"


def _failed_line(failed, head, waited):
    """What the round's comment says of the required checks that failed on `head`, while
    the merge `waited` for them or at its first try."""
    when = " while the merge waited for {}." if waited else ", so GitHub refused the merge."
    if not failed:
        return f"A required check failed on `{head[:7]}`" + when.format("it")
    names = ", ".join(f"`{item['name']}`" for item in failed)
    if len(failed) == 1:
        return f"Required check {names} failed on `{head[:7]}`" + when.format("it")
    return f"Required checks {names} failed on `{head[:7]}`" + when.format("them")


def _head_moved(gh, target, message, workflow, default_branch):
    """Check and review the new head of a pull request whose branch moved after the round
    checked it: the round's approval is for a commit that is no longer its head."""
    try:
        head = gh.pull(target["pr"])["head"]["sha"]
        gh.dispatch(
            workflow,
            default_branch,
            {"command": "review", "number": str(target["pr"]), "note": "", "auto": "true"},
        )
    except GitHubError as error:
        return "merge_refused", f"{message}\n\nNexKit could not start a review: {error}"
    line = (
        f"The branch moved to `{head[:7]}` after this round checked it, so NexKit started "
        "`/nexkit review` on the new commit."
    )
    return "head_moved", f"{message}\n\n{line}"


def _behind(gh, pr):
    """Whether GitHub says the pull request is behind its base branch, which it reports
    only when branch protection wants the branch up to date before a merge."""
    return gh.pull(pr).get("mergeable_state") == "behind"


def _swept(gh, cfg, number):
    """Whether autopilot's sweeps look at this issue, so they can merge its pull request."""
    if not autopilot.takes(cfg):
        return False
    try:
        return autopilot.taken(gh, cfg, gh.issue(number))
    except GitHubError:
        return False


def _update_branch(gh, target, row, message, limit, workflow, default_branch):
    """Bring a branch that branch protection wants up to date with its base branch, and
    check and review the new commit, within `conflicts.max_rounds` per pull request."""
    pr, head, base = target["pr"], target["head"], target["base"]
    updated = [run for _, run in run_comments(gh.comments(pr)) if run.get("updated_branch")]
    if len(updated) >= limit:
        note = f"Its {limit} automatic updates of the branch from `{base}` are used up."
        return "merge_refused", f"{message}\n\n{note}"
    try:
        gh.update_branch(pr, head)
        if target.get("state") is not None:
            # The reporting round's approval, before the review it starts reads it: an
            # implement round's report runs beside that review, in the issue's queue.
            progress.save_state(gh, pr, target.get("state_comment"), target["state"])
            target["state_comment"] = read_state(gh.comments(pr))[0]
        for _ in range(UPDATE_TRIES):
            if gh.pull(pr)["head"]["sha"] != head:
                break
            time.sleep(UPDATE_WAIT)
        # The review carries this round's approval over when the change is the same.
        gh.dispatch(
            workflow,
            default_branch,
            {"command": "review", "number": str(pr), "note": "", "auto": "true,carry"},
        )
    except GitHubError as error:
        return "merge_refused", f"{message}\n\nNexKit could not update the branch: {error}"
    row["updated_branch"] = True
    line = (
        f"It is behind `{base}`, which branch protection does not allow: NexKit merged `{base}` "
        "into it and started `/nexkit review` on the new commit, which checks it and keeps "
        "this approval when the pull request's own change is the same."
    )
    return "branch_updated", f"{message}\n\n{line}"


# Outcomes of an implement round that leave its pull request as it is, with no round on
# its way: a conflict round skipped while it ran starts when it ends.
IMPLEMENT_LEFT = {"ready", "needs_human", "review_failed", "retry_failed", autopilot.CI_WAITING}


def _conflicts_after(gh, cfg, decision, pr, row, workflow, default_branch):
    """Start the conflict round that NexKit left out while this implement round ran, when
    the pull request conflicts with its base branch now. Returns text for the round."""
    try:
        if not is_conflicting(gh, pr):
            return ""
        started = start_conflict_round(
            gh,
            pr,
            decision["base"],
            workflow,
            default_branch,
            cfg["conflicts"]["max_rounds"],
            row=row,
        )
    except GitHubError as exc:
        return f"\n\nCould not check #{pr} for conflicts with `{decision['base']}`: {exc}"
    return f"\n\nIt conflicts with `{decision['base']}`. {started}" if started else ""


def merge_waiting(gh, cfg, step, workflow, default_branch, now=None):
    """Try the merge again for a pull request whose required checks had not passed, from
    an autopilot sweep. A required check that failed meanwhile gets an automatic fix round
    (see `_required_failed`). The round's comment shows what happened. Returns a line for
    the log."""
    pr = step["pull"]
    for comment, run in run_comments(gh.comments(pr)):
        if comment["id"] != step["round_comment"]:
            continue
        target = {"pr": pr, "head": run["head"], "issue": step["number"], "base": step["base"]}
        target["waiting"] = True
        outcome, text = merge_pull(gh, cfg, target, run, "", workflow, default_branch)
        if outcome == "merge_waiting":
            return f"#{pr} still waits for its required checks."
        run.update(outcome=outcome, status=run_status(outcome))
        reason = needs_person(outcome, cfg)
        if reason:
            run["attention"] = reason
        else:
            run.pop("attention", None)
        gh.update_comment(comment["id"], with_run(comment["body"], run, text.strip()))
        notify(gh, cfg, pr, reason)
        return f"#{pr}: {outcome}."
    return f"#{pr}: its round comment is gone."


def close_merged_issue(gh, issue, pr, sha):
    """Close the issue a pull request implements after NexKit merged it.

    GitHub closes it when a person merges, but not after a merge with the Actions token.
    Returns (closed, lines for the round's comment). A failure leaves the merge as it is."""
    try:
        if gh.issue(issue).get("state") != "open":
            return False, []
        commit = f" in {sha}" if sha else ""
        gh.comment(issue, f"NexKit merged #{pr}{commit}, which implements this issue.")
        gh.close_issue(issue, "completed")
    except GitHubError as exc:
        return False, [f"Could not close #{issue}: {exc}. Close it yourself."]
    return True, [f"Closed #{issue} as completed."]


def delete_merged_branch(gh, pr, base):
    """Delete the head branch of a pull request NexKit merged, when the repository deletes
    head branches on merge: GitHub does that after a person's merge, but not after a merge
    with the Actions token. Only a NexKit branch of this repository goes, never the base
    branch, nor one that open pull requests target, which its deletion would close.
    Returns lines for the round's comment. A failure leaves the merge as it is."""
    branch = ""
    try:
        if not gh.deletes_merged_branches():
            return []
        head = gh.pull(pr).get("head") or {}
        branch = head.get("ref") or ""
        own = (head.get("repo") or {}).get("full_name") == gh.repository
        if not own or not branch.startswith(BRANCH_PREFIX) or branch == base:
            return []
        targeting = ", ".join(f"#{pull['number']}" for pull in gh.pulls_into(branch))
        if targeting:
            return [f"Kept branch `{branch}`, the base branch of {targeting}."]
        gh.delete_branch(branch)
    except GitHubError as exc:
        name = f"branch `{branch}`" if branch else "the branch"
        return [f"Could not delete {name}: {exc}. It stays; delete it yourself."]
    return [f"Deleted branch `{branch}`."]


def _record(state, decision, row, agent, review, checks):
    """Keep what the next review and the next fix rounds need: the last review's findings
    and the recent fix rounds, each with the checks that still failed after it."""
    if decision["action"] == "fix":
        fixes = (state.get("fixes") or [])[-(MAX_FIXES - 1) :]
        fixes.append(
            {
                "round": row["round"],
                "head": row["head"],
                "auto": bool(decision.get("auto")),
                "summary": clip(agent.get("summary")),
                "note": clip(decision.get("note"), 2000),
            }
        )
        cause = str((agent.get("output") or {}).get("cause") or "").strip()
        if cause:
            fixes[-1]["cause"] = clip(cause, 2000)
        if decision.get("diagnose"):
            fixes[-1]["diagnosis"] = True
        if checks is not None:
            # Left out when the checks did not run.
            fixes[-1]["failing"] = [
                {"name": name, "signature": signature}
                for name, signature in failing_signatures(checks).items()
            ][:MAX_FINDINGS]
        if agent.get("start_base"):
            fixes[-1]["conflicts"] = (agent.get("conflicts") or [])[:MAX_FINDINGS]
        state["fixes"] = fixes
    if review:
        state["last_review"] = {
            "round": row["round"],
            "head": row["head"],
            "verdict": review["verdict"],
            "findings": [
                {
                    "severity": f["severity"],
                    "file": f.get("file") or "",
                    "line": f.get("line") or 0,
                    "body": clip(f.get("body"), 1500),
                }
                for f in review.get("findings", [])[:MAX_FINDINGS]
            ],
        }


def _ci_wait(gh, cfg, decision, checks, default_branch):
    """`autopilot.ci_wait` for a round that must not fail because of it: without what it
    reads, the round gets the usual fix round."""
    try:
        return autopilot.ci_wait(gh, cfg, decision, checks, default_branch)
    except GitHubError as exc:
        print(
            f"warning: could not compare the checks with the default branch: {exc}", file=sys.stderr
        )
        return None


def report(gh, decision, cfg, needs, artifacts, *, workflow_ref, default_branch):
    """Post the outcome. Returns a short machine-readable summary for logs and tests."""
    result = _report(gh, decision, cfg, needs, Path(artifacts), workflow_ref, default_branch)
    fields = {"outcome": result["outcome"]}
    kept = (
        "resume",
        "attention",
        "agents",
        "timed_out",
        "questions",
        "transient_error",
        "no_change",
        "protected_paths",
    )
    for key in kept:
        if result.get(key):
            fields[key] = result[key]
    progress.finish(gh, decision, run_status(result["outcome"]), result["summary"], **fields)
    wait_for_reset(gh, cfg, result.get("resume_at"), workflow_ref, default_branch)
    notify(
        gh,
        cfg,
        result.get("pr") or decision.get("pr") or decision["issue"],
        result.get("attention"),
    )
    for line in autopilot.after_run(
        gh, cfg, decision, result["outcome"], workflow_file(workflow_ref), default_branch
    ):
        print(line)
    return result


def wait_for_reset(gh, cfg, at, workflow_ref, default_branch):
    """Start the run that waits for a paused command's reset when it comes within
    `usage_limit.wait_minutes`. Later resumes are left to the scheduled check."""
    when = parse_iso(at)
    wait = cfg["usage_limit"]["wait_minutes"]
    if not when or not wait or when > datetime.now(UTC) + timedelta(minutes=wait):
        return
    try:
        start_waiting(gh, workflow_file(workflow_ref), default_branch)
    except GitHubError as exc:
        print(f"warning: could not start waiting for the reset: {exc}", file=sys.stderr)


def carried_over(state, reviewed):
    """The review a round carried over from the approved commit its head merged the base
    branch into, with that review's findings, as the state comment kept them."""
    sha = reviewed["carried"]
    last = state.get("last_review") or {}
    findings = (last.get("findings") or []) if last.get("head") == sha else []
    return {
        "verdict": "approve",
        "summary": f"The review of `{sha[:7]}` carried over.",
        "criteria": [],
        "findings": findings,
        "carried": sha,
    }


def _carry_line(decision, base, carry):
    """What the round's comment says about carrying the approval over, or ""."""
    sha = decision.get("carry_from") or ""
    if not sha or "carried" not in carry:
        # No comparison: the review job did not run, or stopped before it compared.
        return ""
    if carry.get("carried"):
        return (
            f"The AI review of `{sha[:7]}` carried over, so no review agent ran: NexKit merged "
            f"`{base}` into it without a conflict, and the pull request's own change is the "
            "one the review approved."
        )
    why = carry.get("why") or "the changes differ"
    return f"The AI review ran again rather than carry over the approval of `{sha[:7]}`: {why}."


def _report(gh, decision, cfg, needs, artifacts, workflow_ref, default_branch):
    action = decision["action"]
    agent = _load(artifacts / "nexkit-agent" / "result.json")
    publish = json.loads(((needs.get("publish") or {}).get("outputs") or {}).get("result") or "{}")

    if action in ("plan", "implement", "fix"):
        if agent and agent.get("status") == "paused":
            return _paused_agent(gh, decision, cfg, agent)
        if agent and agent.get("status") == "up_to_date":
            message = (
                f"`{decision['base']}` merges cleanly into this pull request now; nothing to do."
            )
            _stop_round(gh, decision, agent, "up_to_date", message)
            return {"outcome": "up_to_date", "summary": message}
        if agent and agent.get("status") == "already_on_base":
            return _already_on_base(gh, cfg, decision, agent)
        failure = _stage_failure(decision, agent, publish)
        if failure:
            outcome, message = failure
            fields = {}
            error = _transient(decision, agent, publish, needs)
            command = progress.COMMANDS[action]
            if error and _retry(
                gh, decision, command, decision["target"], workflow_ref, default_branch
            ):
                outcome, message = "retry", f"{message}\n\n{RETRY_LINE}"
                fields["transient_error"] = clip(one_line(error), 500)
            elif error and decision.get("retry"):
                outcome, message = "retry_failed", _retried(gh, decision, error, message)
            reason = needs_person(outcome, cfg)
            if agent and agent.get("unchanged"):
                reason = _with_finding(reason, agent)
            protected = protected_paths(agent)
            if outcome == "agent_blocked" and protected:
                reason = autopilot.protected_reason(protected)
            if reason and autopilot.carries_on(gh, cfg, decision, outcome, agent):
                reason = None
                message += "\n\nNexKit autopilot carries on from here."
            if decision.get("pr"):
                _stop_round(gh, decision, agent, outcome, message, reason, **fields)
            return {
                "outcome": outcome,
                "summary": message,
                "attention": reason,
                "agents": agents_ran(agent),
                **fields,
                **_autopilot_fields(agent),
            }
        if action == "implement" and publish.get("no_change"):
            return _no_change(gh, cfg, decision, agent)
        if action == "plan":
            plan = publish.get("comment")
            reason = needs_person("planned", cfg, plan=agent.get("output"))
            summary = f"[Plan]({plan}) posted." if plan else ""
            if reason and autopilot.carries_on(gh, cfg, decision, "planned", agent):
                reason = None
                summary += " NexKit autopilot carries on from here."
            return {
                "outcome": "planned",
                "summary": summary.strip(),
                "attention": reason,
                "agents": agents_ran(agent),
            }

    pr = publish.get("pr") or decision["pr"]
    head = publish.get("head") or decision["head"]
    recheck = action == "fix" and bool(publish.get("recheck"))
    checks = _load(artifacts / "nexkit-checks" / "checks.json")
    reviewed = _load(artifacts / "nexkit-review" / "result.json")
    given = reviewed["output"] if reviewed and reviewed.get("status") == "done" else None
    review = apply_criteria(given) if given else None
    confirm = unverified(review)
    review_paused = bool(reviewed) and reviewed.get("status") == "paused"
    url = run_url()

    comments = gh.comments(pr)
    comment_id, state = read_state(comments)
    state = state or empty_state(decision["issue"])
    # Only the review this round's decision named carries over.
    carried = (
        bool(reviewed)
        and reviewed.get("status") == "carried"
        and bool(decision.get("carry_from"))
        and reviewed.get("carried") == decision["carry_from"]
    )
    if carried:
        review = carried_over(state, reviewed)
    carry = _load(artifacts / "nexkit-review" / "carry.json") or {}
    # A round that waited for the default branch is over once another round reports.
    state.pop("ci_wait", None)
    round_id, row = progress.find_round(comments, decision)
    if decision.get("head") and decision["head"] != head:
        _restore_statuses(gh, decision["head"], state.get("feedback"), url)
    resume = None
    if review_paused:
        # The commit is published and checked; only the review runs again after the reset.
        message, resume = pause(reviewed, cfg, "review", pr, resumed=bool(decision.get("resumed")))
        _pause_statuses(gh, head, resume, url)
        checks_ok, verdict = checks is not None and all(c["passed"] for c in checks), None
    else:
        failed = "" if review else review_failure(reviewed)[0]
        checks_ok, verdict = _set_statuses(gh, head, checks, review, url, failed)
    if given:
        gh.create_review(pr, head, review_body({"output": given}, checks))

    row.update(
        head=head,
        checks="not run" if checks is None else ("passed" if checks_ok else "failed"),
        verdict=verdict or "not run",
        tokens=_tokens(agent if action != "review" else None, reviewed),
    )
    _show_agents(row, agent if action != "review" else None, reviewed)
    if carried:
        row["carried"] = reviewed["carried"]
    _record(state, decision, row, agent or {}, review, checks)
    # The checks this round's fix was asked to fix, before they are replaced.
    before = failing_signatures((state.get("feedback") or {}).get("checks"))
    state["feedback"] = {"head": head, "checks": checks, "review": review}
    repeated = [
        name
        for name, signature in failing_signatures(checks).items()
        if action == "fix" and before.get(name) == signature
    ]
    if repeated:
        # Failed the same way before and after this fix: the next automatic round
        # diagnoses them first.
        state["feedback"]["repeated"] = repeated
    run_id = os.environ.get("GITHUB_RUN_ID") or ""
    if checks is not None and run_id.isdigit():
        # The run whose `nexkit-checks` artifact holds the failing checks' result files.
        state["feedback"]["run_id"] = int(run_id)

    if review_paused:
        outcome = "paused"
        row["verdict"] = "paused"
        if resume:
            row["resume"] = resume
    elif checks_ok and verdict == "approve":
        outcome = "ready"
        approved = f"the AI review approved `{head[:7]}`"
        if carried:
            approved = f"the AI review's approval carried over to `{head[:7]}`"
        message = (
            f"✅ Checks passed and {approved}. This pull request is ready for a human decision."
        )
        if confirm:
            held = " NexKit does not merge it by itself." if cfg["merge"]["auto"] else ""
            items = "\n".join(f"- {criterion}" for criterion in confirm)
            message += (
                f"{held}\n\nThe review could not verify these acceptance criteria. Confirm "
                f"them before merging:\n\n{items}"
            )
        elif cfg["merge"]["auto"]:
            target = {"pr": pr, "head": head, "issue": decision["issue"], "base": decision["base"]}
            # A required check that failed gets its fix round through this round's state.
            target.update(state=state, state_comment=comment_id)
            outcome, message = merge_pull(
                gh, cfg, target, row, message, workflow_file(workflow_ref), default_branch
            )
            comment_id = target["state_comment"]
    elif verdict is None:
        outcome = "review_failed"
        message = review_failure(reviewed)[1]
        error = _transient(decision, reviewed, {}, needs, "review")
        if error and _retry(gh, decision, "review", pr, workflow_ref, default_branch):
            outcome, message = "retry", f"{message}\n\n{RETRY_LINE}"
            row["transient_error"] = clip(one_line(error), 500)
        elif error and decision.get("retry"):
            outcome, message = "retry_failed", _retried(gh, decision, error, message)
    elif recheck:
        # The fix agent found nothing to change for a cause outside the code, and the
        # checks and the review ran once more: what still fails waits for a person.
        outcome = "recheck_failed"
        message = (
            "Checks or review still fail on the same commit. Comment `/nexkit fix "
            "<instructions>` or `/nexkit review`, push a commit yourself, or close the PR."
        )
    elif state["auto_fixes"] < cfg["fix"]["max_auto_rounds"] and (
        wait := _ci_wait(gh, cfg, decision, checks, default_branch)
    ):
        # The checks fail as the default branch does: the bug issue about it fixes that,
        # and a fix round here would only fix it again, or change nothing.
        outcome = autopilot.CI_WAITING
        state["ci_wait"] = row["ci_wait"] = wait
        state["feedback"].pop("repeated", None)
        message = autopilot.ci_wait_message(wait)
    elif state["auto_fixes"] < cfg["fix"]["max_auto_rounds"]:
        state["auto_fixes"] += 1
        outcome = "auto_fix"
        message = (
            f"Starting automatic fix round {state['auto_fixes']} of "
            f"{cfg['fix']['max_auto_rounds']} for the failing checks and blocking findings."
        )
    else:
        outcome = "needs_human"
        message = (
            "Checks or review still need attention, and no automatic fix rounds remain. "
            "Comment `/nexkit fix <instructions>`, push a commit yourself, or close the PR."
        )

    cause = str(((agent or {}).get("output") or {}).get("cause") or "").strip()
    if recheck:
        message = (
            "The fix agent changed nothing: the cause lies outside the code. NexKit ran the "
            f"checks and the review again on `{head[:7]}`.\n\n{message}"
        )
    if action == "fix" and cause:
        message = f"**Cause found by the fix agent:** {clip(cause, 1500)}\n\n{message}"
    if action == "fix" and agent and agent.get("merge_held"):
        files = ", ".join(f"`{name}`" for name in agent["merge_held"])
        message = (
            f"Did not merge `{decision['base']}`, which changed {files}: the token NexKit "
            "pushes with may not change workflow files. The pull request stays behind it until "
            "a person merges it, or `publish.push_token_secret` names a token that may.\n\n"
            f"{message}"
        )
    if action == "fix" and agent and agent.get("start_base"):
        files = ", ".join(f"`{name}`" for name in agent.get("conflicts") or [])
        merged = f"Merged `{decision['base']}` (`{agent['start_base'][:7]}`) into the branch"
        done = f" and resolved conflicts in {files}." if files else ", which was behind it."
        message = f"{merged}{done}\n\n{message}"
    carry_line = _carry_line(decision, decision["base"], carry)
    if carry_line:
        message = f"{carry_line}\n\n{message}"
    if action == "implement" and outcome in IMPLEMENT_LEFT and cfg["conflicts"]["auto_resolve"]:
        message += _conflicts_after(
            gh, cfg, decision, pr, row, workflow_file(workflow_ref), default_branch
        )
    row.update(status=run_status(outcome), outcome=outcome)
    reason = needs_person(outcome, cfg, resume=resume, confirm=confirm)
    if outcome == "recheck_failed":
        reason = _with_finding(reason, agent)
    if reason:
        row["attention"] = reason
    progress.save_state(gh, pr, comment_id, state)
    progress.save_round(gh, pr, round_id, row, message)
    if outcome == "auto_fix":
        gh.dispatch(
            workflow_file(workflow_ref),
            default_branch,
            {"command": "fix", "number": str(pr), "note": "", "auto": "true"},
        )
    summary = f"Pull request #{pr}: {message}" if action == "implement" else message
    return {
        "outcome": outcome,
        "pr": pr,
        "head": head,
        "summary": summary,
        "attention": reason,
        "resume_at": resume and resume["at"],
        "agents": row.get("agents"),
    }
