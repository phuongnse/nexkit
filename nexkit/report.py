"""Report a finished pipeline run on GitHub and decide what happens next."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import autopilot, progress, refusal
from .checks import failing_signatures, skipped_line, skipped_names, with_skipped
from .config import CONFIG_PATH, after_workflows
from .github import GitHubError, dispatched_runs_url, run_url
from .maintain import (
    ON_BASE_FAILED_NEED,
    close_on_base,
    close_parents_safely,
    delete_head_branch,
    is_conflicting,
    reach,
    resolve_conflicts,
    start_conflict_round,
    start_waiting,
    update_branch,
)
from .route import auto_input, on_base
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
    option_marks,
    passed_checks,
    passed_lines,
    questions,
    read_state,
    round_questions,
    run_comments,
    with_run,
)
from .usage import iso, parse_iso, shown

ICONS = {True: "✅", False: "❌"}
# A check that NexKit did not run: no path it reads changed since it passed.
SKIPPED_ICON = "⏭️"
CRITERION_MARKS = {"met": "✅", "unmet": "❌", "unverifiable": "❔"}
CHECKLIST_MARKS = {"met": "✅ met", "not_met": "❌ not met", "not_applicable": "➖ not applicable"}
# An item not met that a person decides, which no fix round changes.
PERSON_NOT_MET = "⚖️ not met, a person decides"
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
    "conflicting",
    autopilot.PULL_CLOSED,
    "person_reviewed",
    autopilot.UNTRUSTED_MERGE,
    autopilot.CI_WAITING,
    autopilot.HELD,
}
# What a person does after a run with this outcome, and why NexKit does not, without what
# the run found (see `needs_person`). The other outcomes need nobody: NexKit continues by
# itself (`auto_fix`, `conflict_fix`, a resumed pause, a sweep after `ci_waiting`) or is
# done.
NEEDS_PERSON = {
    "agent_blocked": (
        "answer the questions in the run's comment with `/nexkit plan <answers>`, or "
        "`/nexkit go <answers>` when they leave the plan as it is",
        "the agent stopped at a decision that is the owner's to make",
    ),
    "agent_error": (
        "read the error in the run's comment and log, remove its cause, then post the "
        "command again",
        "the run stopped on an error, and NexKit runs a command again by itself only after "
        "an error of the service, the network or the runner",
    ),
    "agent_failed": (
        "read the agent job's log for why it ended, then post the command again",
        "the agent job ended without a result, so NexKit had nothing to go on",
    ),
    "publish_failed": (
        "read the publish job's log, remove the cause, then post the command again",
        "NexKit could not publish the result",
    ),
    "review_failed": (
        "comment `/nexkit review` to run the review again",
        "the AI review did not complete, and NexKit runs it again by itself only after an "
        "error of the service, the network or the runner",
    ),
    "needs_human": (
        "comment `/nexkit fix <instructions>` for what still fails, push a commit yourself, "
        "or close the pull request",
        "checks or review still need attention, and no automatic fix rounds remain",
    ),
    "merge_refused": (
        "read GitHub's answer in the round's comment, then merge the pull request yourself or "
        "change the rule",
        "GitHub refused the automatic merge",
    ),
    "merge_issue_closed": (
        "merge the pull request or close it",
        "its issue closed before NexKit merged it, and NexKit leaves the pull request of a "
        "closed issue to a person",
    ),
    "retry_failed": (
        "post the command again once the cause has passed",
        "the run stopped on a transient error again after every wait of `retry.wait_minutes`",
    ),
    autopilot.UNTRUSTED_ROUND: (
        autopilot.TRUST_DO,
        "someone without write access edited it after a writer vouched for it, so NexKit does "
        "not act on its text",
    ),
    autopilot.UNTRUSTED_MERGE: (
        autopilot.TRUST_DO,
        "someone without write access edited it after a writer vouched for it, so NexKit does "
        "not merge its pull request",
    ),
    "close_failed": (
        "close the issue yourself",
        "no change is needed, but GitHub refused NexKit's close",
    ),
    "recheck_failed": (
        "check what the fix agent found outside the code, then comment `/nexkit review` once "
        "it works again, or `/nexkit fix` with evidence that the code is the cause",
        "the checks or review still fail on the same commit after the fix agent found a cause "
        "outside the code",
    ),
    "on_base_failed": ON_BASE_FAILED_NEED,
    "conflicting": (
        "comment `/nexkit fix` on the pull request, with any rule the conflict needs",
        "its commit conflicts with the base branch, and NexKit started no round to resolve it",
    ),
}
NO_CHANGE_PLAN = autopilot.person(
    "check the plan's finding, then close the issue, or comment `/nexkit plan <what still "
    "needs changing>`",
    "the plan finds that no change is needed, and NexKit closes an issue by itself only with "
    "autopilot",
)
TOO_LARGE = autopilot.person(
    autopilot.SPLIT_DO, "the plan says the issue is too large for one session"
)
READY = autopilot.person(
    "review the pull request, then merge it or close it",
    "checks and the AI review passed, and with `merge.auto` off a person decides the merge",
)
PAUSED_REASON = autopilot.person(
    "post the command again once the usage limit has reset",
    "the agent hit its usage limit, and NexKit does not resume this run",
)
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


def _not_met(review, person=False):
    """The checklist items the review found not met: those NexKit acts on, or with `person`
    those that a person decides."""
    return [
        c
        for c in (review or {}).get("checklist") or []
        if c["answer"] == "not_met" and bool(c.get("person")) == person
    ]


def apply_criteria(review):
    """The review NexKit acts on. Each unmet criterion and each checklist item not met is a
    blocking finding and requests changes, whatever verdict the reviewer returned. An item
    not met that a person decides is no finding: it goes to `decide`, and when nothing else
    blocks, the review approves, so that no fix round starts and a person's approval
    merges (see `to_decide`)."""
    unmet = _criteria(review, "unmet")
    not_met = _not_met(review)
    decide = [clip(c["item"], 300) for c in _not_met(review, person=True)]
    # Only the checklist decides which items wait for a person.
    review = {key: value for key, value in review.items() if key != "decide"}
    if decide:
        review["decide"] = decide
        blocking = any(f.get("severity") == "blocking" for f in review.get("findings") or [])
        if not unmet and not not_met and not blocking:
            # The reviewer may have requested changes for these items alone.
            review["verdict"] = "approve"
    if not unmet and not not_met:
        return review
    findings = []
    for c in unmet:
        body = f"Acceptance criterion not met: {c['criterion']}. {c.get('evidence') or ''}"
        findings.append({"severity": "blocking", "file": "", "line": 0, "body": body.strip()})
    for c in not_met:
        body = f"Checklist item not met: {c['item']}. {c.get('evidence') or ''}"
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


def to_decide(review):
    """The checklist items, not met, that a person decides before an approving review's
    pull request merges: their approval accepts them (see `maintain.merge_approved`)."""
    if not review or review["verdict"] != "approve":
        return []
    return list(review.get("decide") or [])


def _for_person(cfg, review, confirm, decide):
    """What a ready round's comment says when a person must act before the merge: the
    checklist items they decide, with the review's evidence, and the criteria the review
    could not verify."""
    text = " NexKit does not merge it by itself." if cfg["merge"]["auto"] else ""
    if decide:
        evidence = {
            c["item"]: str(c.get("evidence") or "").strip()
            for c in (review or {}).get("checklist") or []
        }
        items = "\n".join(
            f"- {item}" + (f": {evidence[item]}" if evidence.get(item) else "") for item in decide
        )
        accept = (
            "Approve the pull request to accept them, and NexKit merges it"
            if cfg["merge"]["auto"]
            else "Approve the pull request to accept them, which turns `nexkit/review` green, "
            "then merge it"
        )
        text += (
            "\n\nOnly a person decides these checklist items, which the review found not "
            f"met:\n\n{items}\n\n{accept}. To change the code instead, comment `/nexkit fix "
            "<what to change>`."
        )
    if confirm:
        items = "\n".join(f"- {criterion}" for criterion in confirm)
        text += (
            "\n\nThe review could not verify these acceptance criteria. Confirm them before "
            f"merging:\n\n{items}"
        )
    return text


PERSON_LINE = (
    "NexKit changes nothing on this pull request: a person makes any change, then comments "
    "`/nexkit review` again."
)


def review_body(review, checks, person=False):
    """What a person acts on comes first: verdict, findings, criteria. Evidence is collapsed.
    With `person`, it says who makes any change."""
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
    checklist, checklist_evidence = [], []
    for item in output.get("checklist") or []:
        mark = CHECKLIST_MARKS[item["answer"]]
        if item.get("person") and item["answer"] == "not_met":
            mark = PERSON_NOT_MET
        checklist.append(f"- {mark}: {item['item']}")
        if item.get("evidence"):
            checklist_evidence.append(f"- {mark}: {item['item']}: {item['evidence'].strip()}")

    lines = [
        f"### NexKit review: {'approve' if approved else 'changes requested'}",
        "",
        output["summary"].strip(),
    ]
    if person:
        lines += ["", PERSON_LINE]
    if given["verdict"] == "approve" and not approved:
        what = "an acceptance criterion" if _criteria(given, "unmet") else "a checklist item"
        lines += ["", f"NexKit requests changes because {what} is not met."]
    if output.get("decide") and not person:
        lines += [
            "",
            "A person decides the checklist items that are not met here (⚖️), such as one "
            "that accepts a risk. NexKit starts no fix round for them, and merges only once a "
            "person with write access approves this commit.",
        ]
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
    lines += _part("Checklist", checklist)
    if checks:
        lines += [
            "",
            "**Checks**: "
            + ", ".join(
                f"{SKIPPED_ICON if c.get('skipped') else ICONS[c['passed']]} `{c['name']}`"
                for c in checks
            ),
        ]
    evidence = _part("Acceptance criteria", criteria_evidence)
    evidence += _part("Checklist", checklist_evidence)
    evidence += _part("Previous findings", previous_evidence)
    if evidence:
        # GitHub renders Markdown inside <details> only after a blank line.
        lines += ["", "<details><summary>Evidence</summary>", *evidence, "", "</details>"]
    return "\n".join(lines)


def workflow_file(workflow_ref):
    """`owner/repo/.github/workflows/nexkit.yml@refs/heads/main` -> `nexkit.yml`."""
    return workflow_ref.split("@", 1)[0].rsplit("/", 1)[-1]


def config_check(error):
    """The failed check of a person's pull request whose `.nexkit/config.json` NexKit would
    refuse (see `route.head_config_error`)."""
    return {
        "name": CONFIG_PATH,
        "run": "nexkit doctor",
        "exit_code": 1,
        "passed": False,
        "seconds": 0,
        "output": error,
    }


def config_status(error):
    """The `nexkit/checks` description for that pull request."""
    text = f"NexKit would refuse {CONFIG_PATH}: {one_line(error)}"
    return text if len(text) <= STATUS_LIMIT else text[: STATUS_LIMIT - 1] + "…"


def config_line(error, head):
    """What the round's comment says of it."""
    return (
        f"❌ NexKit would refuse the `{CONFIG_PATH}` of `{head[:7]}`: {clip(one_line(error), 500)}"
        "\n\nNexKit reads its configuration from the default branch, so once this merged it "
        "would refuse every command, the one on the pull request that corrects it included. "
        "`nexkit doctor` shows the same error; correct the file, then comment `/nexkit review` "
        "again."
    )


def _set_statuses(gh, head, checks, review, url, review_error="", config_error=""):
    """Set both commit statuses from check results and a review output (None: not run).
    `review_error` says why a review that did not complete stopped; `config_error` why
    NexKit would refuse the configuration the pull request brings."""
    checks_ok = checks is not None and all(c["passed"] for c in checks)
    if config_error:
        text = config_status(config_error)
    elif checks_ok:
        text = "Checks passed"
    else:
        text = "Checks did not run" if checks is None else "Some checks failed"
    gh.set_status(head, "nexkit/checks", "success" if checks_ok else "failure", text, url)
    verdict = review["verdict"] if review else None
    if verdict:
        if verdict != "approve":
            text = "AI review requested changes"
        elif to_decide(review):
            count = len(to_decide(review))
            items = "item" if count == 1 else "items"
            text = f"A person decides {count} checklist {items} that the review found not met"
        elif review.get("carried"):
            text = f"AI review of {review['carried'][:7]} carried over; the change is the same"
        elif unverified(review):
            text = "AI review approved; a person confirms the criteria it could not verify"
        else:
            text = "AI review approved"
        # Items a person decides hold the merge until their approval, which turns it green.
        passed = verdict == "approve" and not to_decide(review)
        gh.set_status(head, "nexkit/review", "success" if passed else "failure", text, url)
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
    """An agent's question's options, with their marks."""
    found = questions([item])
    if not found:
        return []
    return [f"{indent}- {option}{option_marks(found[0], option)}" for option in found[0]["options"]]


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
    if finding and agent.get("stage") == "implement" and any(q.get("closes") for q in found):
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
            "attention": needs_person("close_failed", cfg, found={"error": str(exc)}),
            "agents": agents_ran(agent),
        }
    lines = [f"No change is needed, so NexKit closed #{issue} as completed.", "", finding]
    if cfg["close_parent_issues"]:
        # At once, for this round's comment; the `issues: closed` run finds nothing left.
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
        return "publish_failed", f"NexKit could not publish the {action} result: {reason}"
    return None


def _failure_error(agent, publish):
    """The error of the agent or publish stage that stopped, or ""."""
    if agent and agent.get("status") not in ("done", "blocked"):
        return str(agent.get("error") or "")
    return str(publish.get("error") or "") if agent else ""


def _with_finding(reason, agent):
    """`reason` with what the fix agent found when it changed nothing, so that a person
    sees it in the mention and in `nexkit status`."""
    output = (agent or {}).get("output") or {}
    found = str(output.get("cause") or output.get("summary") or "").strip()
    if not reason or not found:
        return reason
    return f"{reason.rstrip('.')}; the agent found: {autopilot.quoted(found, 300)}"


def _listed(items, size=100):
    """The first of `items` in code spans, then how many more there are."""
    first = autopilot.quoted(items[0], size)
    return first + (f" and {len(items) - 1} more" if len(items) > 1 else "")


def needs_person(outcome, cfg, plan=None, resume=None, confirm=(), decide=(), found=None):
    """The reason a person is needed after a run, or None: what to do, then why NexKit does
    not, with what the run found that shows where to look (see `autopilot.person`). It
    follows only the outcome and the run's structured output, never a judgment by the
    agent. `confirm` holds the criteria a ready round's review could not verify, and
    `decide` the checklist items not met that a person decides. `found`
    may name `questions` (a blocked round's), `pull` (the run is a pull request's round),
    `error`, `failing` (the names of checks that still fail), `findings` (the review's
    blocking findings), `refused` (`refusal.explain` for a refused merge) and
    `timed_out`."""
    found = found or {}
    if outcome == "planned":
        plan = plan or {}
        if plan.get("too_large"):
            return TOO_LARGE
        if filled(plan.get("no_change")):
            return NO_CHANGE_PLAN
        if plan.get("protected_paths"):
            return autopilot.protected_reason(plan["protected_paths"])
        if not plan.get("questions"):
            return None
        asked = questions(plan["questions"])
        why = "the plan asks for decisions that are the owner's to make"
        if asked:
            why += ": " + _listed([item["question"] for item in asked])
        return autopilot.person("answer the plan's questions with `/nexkit plan <answers>`", why)
    if outcome == "merge_waiting":
        # Only a round whose issue autopilot's sweeps look at waits like this.
        return None
    if outcome == "ready":
        if decide:
            accept = (
                "approve the pull request to accept them, which NexKit then merges"
                if cfg["merge"]["auto"]
                else "approve the pull request to accept them, then merge it"
            )
            confirmed = " and confirm the criteria the review could not verify" * bool(confirm)
            return autopilot.person(
                f"decide the checklist items in the round's comment{confirmed}: {accept}, or "
                "comment `/nexkit fix <what to change>`",
                "only a person decides " + _listed(list(decide)) + ", which the review found "
                "not met",
            )
        if confirm:
            then = (
                "approve the pull request, which NexKit then merges"
                if cfg["merge"]["auto"]
                else "merge the pull request"
            )
            return autopilot.person(
                "confirm from the diff and the checks the acceptance criteria that the review "
                f"could not verify, then {then}",
                "the review could not verify " + _listed(list(confirm)),
            )
        return None if cfg["merge"]["auto"] else READY
    if outcome == "paused":
        return None if resume else PAUSED_REASON
    if outcome not in NEEDS_PERSON:
        return None
    do, why = NEEDS_PERSON[outcome]
    if outcome == "retry_failed" and found.get("stops") == 1:
        why = (
            "the run stopped on a transient error, and with `retry.wait_minutes` empty NexKit "
            "does not run a command again"
        )
    elif outcome == "retry_failed" and found.get("stops"):
        why = (
            f"the run stopped on a transient error {_times(found['stops'])}, the last after "
            "every wait of `retry.wait_minutes`"
        )
    if outcome == "agent_blocked" and found.get("pull"):
        do = "answer the questions in the round's comment with `/nexkit fix <answers>`"
    if outcome == "agent_blocked" and found.get("questions"):
        why += ": " + _listed([item["question"] for item in questions(found["questions"])])
    if outcome == "agent_error" and found.get("timed_out"):
        do = (
            "comment `/nexkit plan` to split the issue into parts that each fit one session, "
            "or raise the stage's `timeout_minutes`"
        )
        why = "the agent ran out of time before it finished"
    elif outcome in ("agent_error", "publish_failed", "close_failed", "retry_failed"):
        if found.get("error"):
            why += ": " + autopilot.quoted(found["error"], 200)
    if outcome == "review_failed" and found.get("error"):
        if ACCESS_ERROR.search(found["error"]):
            do = "fix the review agent's secret, then comment `/nexkit review`"
            why = "the AI review could not sign in, and another try stops the same way"
        why += ": " + autopilot.quoted(found["error"], 200)
    if outcome == "needs_human":
        failing = [autopilot.quoted(name, 60) for name in found.get("failing") or []]
        what = []
        if failing:
            what.append(f"check{'s' if len(failing) > 1 else ''} {', '.join(failing[:5])} fail")
        findings = found.get("findings") or []
        if findings:
            plural = "s" if len(findings) > 1 else ""
            first = findings[0]
            where = first.get("file") or ""
            where += f":{first['line']}" if where and first.get("line") else ""
            at = f", the first at {autopilot.quoted(where, 120)}" if where else ""
            what.append(f"the review has {len(findings)} blocking finding{plural}{at}")
        if what:
            why = " and ".join(what) + ", and no automatic fix rounds remain"
    if outcome == "merge_refused" and found.get("refused"):
        do, why = found["refused"]
    return autopilot.person(do, why)


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


def _times(count):
    return "once" if count == 1 else "twice" if count == 2 else f"{count} times"


def run_status(outcome):
    if outcome in ("paused", autopilot.RETRY_WAITING):
        # It runs again after the reset, or after the wait of `retry.wait_minutes`.
        return PAUSED
    if outcome == "retry":
        # The command runs once more in a run of its own.
        return CONTINUED
    return SUCCESS if outcome in COMPLETED else FAILURE


def _transient(decision, agent, publish, needs, stage="agent"):
    """The error of a stage that stopped for a reason another try may pass, or None: a
    transient error of the agent or of GitHub while `publish` wrote, a job that failed as a
    whole, or, in a round NexKit started itself, a branch that moved while the agent
    worked. A job a person cancelled is never one."""
    result = (needs.get(stage) or {}).get("result")
    if result == "cancelled":
        return None
    if result == "skipped":
        # The review runs only after the checks: when their job failed as a whole, that
        # job is what another try may pass.
        for job in ("shards", "verify", "combine"):
            if (needs.get(job) or {}).get("result") == "failure":
                return f"the {job} job ended without a result"
        return None
    if agent is None:
        return f"the {stage} job ended without a result"
    if agent.get("status") == "error" and agent.get("transient"):
        return agent.get("error") or "a transient error"
    if stage == "agent" and publish.get("moved") and _automatic(decision):
        return publish.get("error")
    if stage == "agent" and not publish and (needs.get("publish") or {}).get("result") == "failure":
        # The publish job ended without its outcome, as when it ran out of time while GitHub
        # failed, or lost its runner.
        return "the publish job ended without a result"
    if stage == "agent" and publish.get("transient"):
        # Every try of a push or a GitHub write failed on a server error, a rate limit or
        # a dropped connection (see `publish.WAITS`).
        return publish.get("error") or "a transient error of GitHub"
    return None


def _automatic(decision):
    return bool(decision.get("auto") or decision.get("autopilot"))


def _retries_left(cfg, decision):
    """How many runs again after a transient error `retry.wait_minutes` still allows."""
    return max(0, len(cfg["retry"]["wait_minutes"]) - int(decision.get("retry") or 0))


def _retry(gh, cfg, decision, command, number, workflow_ref, default_branch, now=None):
    """Start `command` again after a transient error, with the same note for the same
    command, after the next wait of `retry.wait_minutes`: at once when it is 0, otherwise
    later, by `maintain.resume_paused`. Returns the inputs of the run it started, for the
    run's `next` (see `autopilot._handed_on`), or for one that waits, its resume record
    with `at` and `retry`, its number. None when the waits are used up, or the run could
    not start."""
    if not _retries_left(cfg, decision):
        return None
    number_again = int(decision.get("retry") or 0) + 1
    same = command == progress.COMMANDS[decision["action"]]
    note = (decision.get("note") or "") if same else ""
    if same:
        auto = auto_input(decision, retry=number_again)
    else:
        auto = auto_input({"autopilot": decision.get("autopilot")}, retry=number_again)
    inputs = {"command": command, "number": str(number), "note": note, "auto": auto}
    wait = cfg["retry"]["wait_minutes"][number_again - 1]
    if wait:
        at = (now or datetime.now(UTC)) + timedelta(minutes=wait)
        return {**inputs, "at": iso(at), "retry": number_again}
    try:
        gh.dispatch(workflow_file(workflow_ref), default_branch, inputs)
    except GitHubError as exc:
        print(f"warning: could not start the command again: {exc}", file=sys.stderr)
        return None
    return inputs


def _earlier_errors(gh, decision):
    """The transient errors that the runs before this run again stopped on, oldest first:
    one for each run since the person's or NexKit's command they repeat. A review that a
    stage of `/nexkit go` handed on keeps that stage's errors on the issue."""
    count = int(decision.get("retry") or 0)
    if not count:
        return []
    numbers = [decision.get("pr"), decision["issue"]] if decision.get("pr") else [decision["issue"]]
    found = []
    for number in dict.fromkeys(n for n in numbers if n):
        for comment, run in run_comments(gh.comments(number)):
            if run.get("transient_error") and run.get("url") != run_url():
                found.append((comment.get("created_at") or "", comment["id"], run))
    found.sort(key=lambda item: item[:2])
    errors = []
    for _, _, run in found:
        # A `/nexkit go` that went on in its pull request shows its error in both places.
        if not errors or errors[-1][0] != run.get("url"):
            errors.append((run.get("url"), run["transient_error"]))
    return [error for _, error in errors[-count:]]


def errors_text(errors):
    """The transient errors of a command's runs so far, numbered, for its comment."""
    lines = [f"{i}. {clip(one_line(error), 500)}" for i, error in enumerate(errors, 1)]
    return "Transient errors so far:\n\n" + "\n".join(lines)


# An implement round that changed files and also filled `no_change` (see
# `agent.set_aside_finding`): the text is a note, and the issue stays open.
NO_CHANGE_NOTE = "**Note from the implement agent:** "
NO_CHANGE_KEPT = (
    "The agent wrote this note as a finding that no change is needed, but it changed files, "
    "so NexKit published the change and keeps the issue open."
)


RETRY_LINE = (
    "The error looks transient, from the service, the network or the runner rather than the "
    "work, so NexKit runs the command {when}."
)
# A session that gave no result that fits (`agent.unfit`), even when asked for it alone.
UNFIT_RETRY_LINE = (
    "The agent gave no result that fits{asked}. Another run may give one, so NexKit runs "
    "the command {when}."
)


def retry_line(result, again=None, total=1):
    """Why NexKit runs a stage's command again after `result`, and when: `again` is what
    `_retry` returned, and `total` how many runs again `retry.wait_minutes` allows."""
    result, again = result or {}, again or {}
    number = again.get("retry") or 1
    if again.get("at"):
        when = f"again after {shown(again['at'])}: run again {number} of {total}"
    elif total > 1:
        when = f"again now: run again {number} of {total}"
    else:
        when = "once more"
    if not result.get("unfit"):
        line = RETRY_LINE.format(when=when)
    else:
        asked = ", even when asked for it alone" * bool(result.get("asked_again"))
        line = UNFIT_RETRY_LINE.format(asked=asked, when=when)
    if again.get("at"):
        line += " A new command here replaces this."
    return line


def _retrying(gh, cfg, decision, result, error, again, message):
    """(outcome, message, fields) of a run whose command runs again after a transient
    error, as `_retry` started it or made it wait. The fields go into its marker: its
    error, for the next run's list, and what it started or waits to start."""
    total = len(cfg["retry"]["wait_minutes"])
    message = f"{message}\n\n{retry_line(result, again, total)}"
    errors = [*_earlier_errors(gh, decision), error]
    if len(errors) > 1:
        message += f"\n\n{errors_text(errors)}"
    fields = {"transient_error": clip(one_line(error), 500)}
    if again.get("at"):
        return autopilot.RETRY_WAITING, message, {**fields, "resume": again}
    return "retry", message, {**fields, "next": again}


def _retried(gh, decision, error, message):
    """The message of a run that stopped on a transient error with no run again left, with
    every error of the command's runs so far."""
    errors = [*_earlier_errors(gh, decision), error]
    if len(errors) > 1:
        message += f"\n\n{errors_text(errors)}"
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
    """Start each workflow of `workflows`, entries as `config.after_workflows` gives them,
    on the base branch after NexKit merged into it, when it is due (see `_due`).

    The merge's push starts the workflows that listen to it by themselves; these are the
    ones a repository starts only every few merges, to save runners. A dispatch that fails
    is reported; the merge stays, and the next merge starts the workflow, which is still
    due then. Each start is added to `records`, for the round's marker.
    """
    lines = []
    records = [] if records is None else records
    for entry in workflows:
        workflow, every = entry["workflow"], entry["every"]
        waits = _due(gh, workflow, every, base)
        if waits:
            lines.append(f"- `{workflow}` waits: {waits}")
            continue
        try:
            run = gh.dispatch(workflow, base, {}, run_details=True) or {}
            url = run.get("html_url") or dispatched_runs_url(workflow, base)
            lines.append(f"- Started [`{workflow}`]({url})")
            records.append({"workflow": workflow, "run_id": run.get("workflow_run_id"), "url": url})
        except GitHubError as exc:
            lines.append(f"- Could not start `{workflow}`: {exc}")
            records.append({"workflow": workflow, "error": str(exc)[:300]})
    return "\n".join([f"After the merge, on `{base}`:", *lines]) if lines else ""


# The runs of a workflow on the base branch that `_due` reads: runs of pull requests from
# forks whose branch has the same name may be among them.
AFTER_MERGE_RUNS = 20
# A run in one of these hasn't finished. One that waits for a person, such as for the
# approval of a deployment, does not hold the next start back.
UNFINISHED = {"requested", "queued", "pending", "in_progress"}


def _due(gh, workflow, every, base):
    """None when `workflow` should start on `base` after a merge, or why it waits.

    A workflow with `every` 1 starts after every merge. Otherwise NexKit keeps no count of
    its own: it counts the commits on the base branch since the head of the workflow's
    latest run there, whatever started it, so a person's push resets the count. It starts
    once the count reaches `every`, and also when in doubt: without a run on the base
    branch, when that run's head is no longer on it, or when GitHub could not tell. A run
    on the base branch that is queued or in progress holds every start back."""
    if every == 1:
        return None
    try:
        runs = [
            run
            for run in gh.workflow_runs(workflow, base, AFTER_MERGE_RUNS)
            if autopilot.on_default_branch(gh, run)
        ]
    except GitHubError:
        return None
    if any(run.get("status") in UNFINISHED for run in runs):
        return f"a run on `{base}` has not finished yet."
    if not runs or not runs[0].get("head_sha"):
        return None
    try:
        comparison = gh.compare(runs[0]["head_sha"], base)
    except GitHubError:
        return None
    if comparison.get("behind_by", 0) or comparison.get("status") not in ("ahead", "identical"):
        return None
    count = comparison.get("ahead_by", 0)
    if count >= every:
        return None
    return f"{count} of {every} commits since its [latest run]({runs[0].get('html_url', '')})."


# What GitHub says when it refuses a merge for required checks that have not reported yet.
# NexKit reads this sentence, which GitHub does not document, only when its data cannot
# say: the token may not read classic branch protection, which names the required checks,
# and GitHub lists a required check for a pull request only once it has reported.
CHECKS_PENDING = re.compile(
    r"required status checks? .*\b(?:is|are) (?:expected|pending|in progress|queued)", re.I
)
# How often a merge refused because the base branch was modified is tried, and the seconds
# before the first try again; each later try waits that much longer.
MERGE_TRIES = 3
MERGE_DELAY = 10


def merge_pull(gh, cfg, target, row, message, workflow, default_branch):
    """Merge a pull request that checks and review approved: `target` names its `pr`,
    `head`, `issue` and `base`. Returns (outcome, message), `message` being the round's
    text so far. A merge refused because the base branch was modified meanwhile is tried
    again a few times; other refusals that pass are handed on (see `_refused`). A pull
    request whose base branch already holds its change is closed instead, with its issue
    (see `maintain.close_on_base`). One that a person merged or closed first ends the round
    as `pull_closed`, before its issue counts, since that merge closes the issue too."""
    pr, head = target["pr"], target["head"]
    number = pr
    try:
        ended = _ended(gh.pull(pr))
        number = target["issue"]
        issue = gh.issue(number)
        closed = issue.get("state") != "open"
        edited = autopilot.edited_after_vouch(gh, issue)
    except GitHubError as exc:
        message += f" NexKit did not merge it: it could not read #{number}: {exc}"
        target["found"] = {
            "refused": (
                "comment `/nexkit review` on the pull request to try the merge again",
                f"NexKit could not read #{number}: {autopilot.quoted(str(exc), 200)}",
            )
        }
        return "merge_refused", message.strip()
    if ended:
        # A person merged or closed it first, which may also have closed its issue: there
        # is nothing left to merge, and nobody to ask.
        return autopilot.PULL_CLOSED, _passed_but(head, ended)
    if closed:
        # A person closed the issue while the pull request waited: they decide its fate.
        message += (
            f" NexKit did not merge it: #{target['issue']} is closed. Merge the pull request "
            "or close it yourself."
        )
        return "merge_issue_closed", message.strip()
    if edited:
        message += (
            f" NexKit did not merge it: {edited}. Read #{target['issue']}, and comment "
            f"`/nexkit trust` there if you trust it; NexKit then reviews this pull request again."
        )
        return autopilot.UNTRUSTED_MERGE, message.strip()
    merge_base = on_base(gh, target["base"], head)
    if merge_base:
        outcome, text, commit = close_on_base(gh, cfg, {**target, "merge_base": merge_base})
        if commit:
            row["on_base"] = commit
        return outcome, f"✅ Checks and AI review passed on `{head[:7]}`.\n\n{text}"
    for attempt in range(MERGE_TRIES):
        tip = _tip(gh, target["base"])
        try:
            merge = gh.merge(pr, head) or {}
            break
        except GitHubError as exc:
            # Another merge into the base branch at the same moment: the next try may pass.
            moved = tip is not None and _tip(gh, target["base"]) not in (None, tip)
            if attempt + 1 < MERGE_TRIES and moved:
                time.sleep(MERGE_DELAY * (attempt + 1))
                continue
            return _refused(gh, cfg, target, row, message, exc, workflow, default_branch)
    message = f"✅ Checks and AI review passed; merged `{head[:7]}`."
    if attempt:
        message += f" GitHub took it at try {attempt + 1}, after the base branch moved."
    closed, lines = close_merged_issue(gh, target["issue"], pr, merge.get("sha"))
    if closed and cfg["close_parent_issues"]:
        # At once, for this round's comment; the `issues: closed` run finds nothing left.
        lines += close_parents_safely(gh, target["issue"])
    lines += delete_head_branch(gh, pr)
    runs = []
    started = after_merge(gh, after_workflows(cfg), target["base"], runs)
    if runs:
        row["after_merge"] = runs
    if cfg["conflicts"]["auto_resolve"]:
        # At once, and also without a `push` trigger. A `push` run that starts the same
        # round adds nothing: `route` drops a second round on one base commit.
        swept = resolve_conflicts(
            gh,
            target["base"],
            workflow,
            default_branch,
            cfg["conflicts"]["max_rounds"],
            {pr},
            cfg=cfg,
        )
        started = "\n".join(filter(None, [started, *swept]))
    parts = ["\n".join(lines), started]
    return "merged", message + "".join(f"\n\n{part}" for part in parts if part)


def _ended(pull):
    """What happened to a pull request that is no longer open, as a sentence for its round,
    or None for an open one."""
    if pull.get("state", "open") == "open":
        return None
    login = (pull.get("merged_by") or {}).get("login")
    by = f" by `{login}`" if login else ""
    if pull.get("merged") or pull.get("merged_at"):
        return f"It was already merged{by}, so NexKit had nothing to merge."
    return "It was already closed, so NexKit did not merge it."


def _passed_but(head, ended):
    return f"✅ Checks and AI review passed on `{head[:7]}`. {ended}"


def _refused(gh, cfg, target, row, message, exc, workflow, default_branch):
    """(outcome, message) after GitHub refused the merge: a conflict round for a conflict,
    a review of a head that moved, an automatic fix round for required checks that failed,
    at the first try or while the merge waits (see `_required_failed`), an update of a
    branch that is behind with a review of the new commit, `merge_waiting` for required
    checks that have not passed yet, which autopilot's next sweeps merge, and otherwise
    `merge_refused` for a person. GitHub's data decides: the response's status, the pull
    request, the base branch's required checks and the head's checks. The error's text
    counts only for required checks that have not reported, when the data cannot say."""
    pr, base, head = target["pr"], target["base"], target["head"]
    try:
        ended = _ended(gh.pull(pr))
    except GitHubError:
        ended = None
    if ended:
        # A person merged or closed it while NexKit tried, which GitHub then refused.
        return autopilot.PULL_CLOSED, _passed_but(head, ended)
    message += f" Automatic merge was not possible: {exc}"
    auto_resolve, limit = cfg["conflicts"]["auto_resolve"], cfg["conflicts"]["max_rounds"]
    if auto_resolve and is_conflicting(gh, pr):
        started = start_conflict_round(gh, pr, base, workflow, default_branch, limit, row=row)
        if started.startswith("Started"):
            return "conflict_fix", f"{message}\n\nIt conflicts with `{base}`. {started}"
        target["found"] = {
            "refused": (
                "comment `/nexkit fix` on the pull request, with any rule the conflict needs",
                f"it conflicts with `{base}`, and NexKit started no round to resolve it"
                + (f": {autopilot.quoted(started)}" if started else ""),
            )
        }
        return "merge_refused", f"{message}\n\n{started}" if started else message
    if exc.status == 409 or _head(gh, pr) not in (None, head):
        # GitHub answers 409 when the head is no longer the commit the round checked.
        return _head_moved(gh, target, row, message, workflow, default_branch)
    failed, pending, complete = required_checks(gh, base, pr, head)
    if failed:
        return _required_failed(gh, cfg, target, row, message, failed, workflow, default_branch)
    state = merge_state(gh, pr)
    if auto_resolve and state == "behind":
        # Checks that passed on a head behind its base count as "expected" when branch
        # protection wants it up to date: they never pass without an update.
        return _update_branch(gh, target, row, message, limit, workflow, default_branch)
    unreported = not complete and state in ("blocked", "behind") and CHECKS_PENDING.search(str(exc))
    if pending or unreported:
        if not _swept(gh, cfg, target["issue"]):
            line = "Required checks have not passed yet. Merge it once they pass."
            names = ", ".join(autopilot.quoted(name, 60) for name in pending[:5])
            target["found"] = {
                "refused": (
                    "merge the pull request once its required checks pass",
                    f"its required checks{f' ({names})' if names else ''} had not passed "
                    "yet, and autopilot's sweeps, which merge it then, do not look at its issue",
                )
            }
            return "merge_refused", f"{message}\n\n{line}"
        row.setdefault("waiting_since", iso(datetime.now(UTC)))
        line = (
            "Required checks have not passed yet. The next autopilot sweep merges it once "
            "they pass."
        )
        return "merge_waiting", f"{message}\n\n{line}"
    if state == "clean":
        # GitHub calls it mergeable and still refuses it: sweeps do not try this head again.
        row["refused_clean"] = head
    if state in ("dirty", "behind"):
        # Only without `conflicts.auto_resolve` does a refusal for these get here.
        do, why = _not_up_to_date(state, base)
    else:
        do, why = refusal.explain_safely(gh, target, exc)
    target["found"] = {"refused": (do, why)}
    return "merge_refused", f"{message}\n\n**What unblocks it:** {autopilot.person(do, why)}."


def _not_up_to_date(state, base):
    """(do, why) for a pull request that conflicts with its base branch (`dirty`), or is
    behind it where the rules want it up to date (`behind`), with `conflicts.auto_resolve`
    off."""
    if state == "dirty":
        return (
            "comment `/nexkit fix` on the pull request, with any rule the conflict needs",
            f"it conflicts with `{base}`, and with `conflicts.auto_resolve` off NexKit starts "
            "no round to resolve it",
        )
    return (
        f"merge `{base}` into the pull request's branch, then comment `/nexkit review`",
        f"the rules of `{base}` want pull requests up to date, and with "
        "`conflicts.auto_resolve` off NexKit does not update the branch",
    )


def required_checks(gh, base, pr, head):
    """(failed, pending, complete): the names of the checks that the merge of pull request
    `pr` needs and that failed on `head`, of those that have not reported on it or still
    run, and whether the names are all of them. The names come from the base branch's
    rulesets and classic branch protection, checked against the head's check runs and
    commit statuses, and from the checks GitHub marks as required on the pull request,
    which it lists only once they reported."""
    failed, pending = set(), set()
    try:
        names, complete = gh.required_checks(base)
    except GitHubError:
        names, complete = set(), False
    if names:
        try:
            runs = {run.get("name"): run for run in gh.check_runs(head)}
            statuses = {item.get("context"): item for item in gh.commit_statuses(head)}
        except GitHubError:
            names, complete = set(), False
        for name in names:
            run, status = runs.get(name), statuses.get(name)
            if run:
                # A check run has a conclusion once it completed.
                if not run.get("conclusion"):
                    pending.add(name)
                elif run["conclusion"] in autopilot.FAILED:
                    failed.add(name)
            elif status:
                if status.get("state") == "pending":
                    pending.add(name)
                elif status.get("state") in ("failure", "error"):
                    failed.add(name)
            else:
                pending.add(name)
    try:
        contexts = [c for c in gh.required_contexts(pr) if c.get("sha") == head]
    except GitHubError:
        contexts = []
    failed |= {c["name"] for c in contexts if c["state"] == "failed"}
    pending |= {c["name"] for c in contexts if c["state"] == "pending"}
    return sorted(failed), sorted(pending - failed), complete


def merge_state(gh, pr, tries=6, delay=5):
    """The pull request's `mergeable_state`, such as "behind" or "blocked", or None. GitHub
    works it out in the background after a branch moves and says "unknown" until then."""
    for attempt in range(tries):
        try:
            state = gh.pull(pr).get("mergeable_state")
        except GitHubError:
            return None
        if state != "unknown":
            return state
        if attempt < tries - 1:
            time.sleep(delay)
    return None


def _tip(gh, branch):
    try:
        return gh.branch_sha(branch)
    except GitHubError:
        return None


def _head(gh, pr):
    try:
        return gh.pull(pr)["head"]["sha"]
    except (GitHubError, KeyError, TypeError):
        return None


def _required_failed(gh, cfg, target, row, message, names, workflow, default_branch):
    """(outcome, message) after required checks failed, at the round's first try of the
    merge or while it waited for them: an automatic fix round within
    `fix.max_auto_rounds`, as after failing checks of a round, unless they fail as the
    default branch does while autopilot's bug issue about that is open (`ci_waiting`, see
    `autopilot.ci_wait`). The state comment keeps the failed checks for the fix round,
    which merges a base branch that has moved first.

    At the first try, `target["state"]` is the state that the reporting round saves, and
    the round starts the fix round for the `auto_fix` outcome; here it is only updated."""
    pr, head = target["pr"], target["head"]
    failed = autopilot.failed_checks(gh, head, names)
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
        target["found"] = {"failing": [item["name"] for item in failed]}
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
        row["next"] = inputs
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


def _head_moved(gh, target, row, message, workflow, default_branch):
    """Check and review the new head of a pull request whose branch moved after the round
    checked it: the round's approval is for a commit that is no longer its head."""
    try:
        head = gh.pull(target["pr"])["head"]["sha"]
        inputs = {"command": "review", "number": str(target["pr"]), "note": "", "auto": "true"}
        gh.dispatch(workflow, default_branch, inputs)
        row["next"] = inputs
    except GitHubError as error:
        target["found"] = {
            "refused": (
                "comment `/nexkit review` on the pull request",
                "its branch moved after the round checked it, and NexKit could not start a "
                f"review: {autopilot.quoted(str(error), 200)}",
            )
        }
        return "merge_refused", f"{message}\n\nNexKit could not start a review: {error}"
    line = (
        f"The branch moved to `{head[:7]}` after this round checked it, so NexKit started "
        "`/nexkit review` on the new commit."
    )
    return "head_moved", f"{message}\n\n{line}"


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
    pr, base = target["pr"], target["base"]

    def save():
        if target.get("state") is not None:
            # The reporting round's approval, before the review it starts reads it: an
            # implement round's report runs beside that review, in the issue's queue.
            progress.save_state(gh, pr, target.get("state_comment"), target["state"])
            target["state_comment"] = read_state(gh.comments(pr))[0]

    why = update_branch(gh, target, row, limit, workflow, default_branch, before_review=save)
    if why:
        target["found"] = {
            "refused": (
                f"merge `{base}` into the pull request's branch, then comment `/nexkit review`",
                f"`{base}` wants pull requests up to date, and NexKit did not update it: "
                + autopilot.quoted(why),
            )
        }
        return "merge_refused", f"{message}\n\n{why}"
    line = (
        f"It is behind `{base}`, which branch protection does not allow: NexKit merged `{base}` "
        "into it and started `/nexkit review` on the new commit, which checks it and keeps "
        "this approval when the pull request's own change is the same."
    )
    return "branch_updated", f"{message}\n\n{line}"


def _held(gh, cfg, number):
    """The label that holds issue `number`, or None (see `autopilot.held`), for a round that
    must not fail because of it."""
    try:
        return autopilot.held(gh, cfg, number)
    except GitHubError as exc:
        # Held, to be safe: the next sweep starts what this held back once it can read
        # that no label holds the issue.
        print(f"warning: could not read whether #{number} is on hold: {exc}", file=sys.stderr)
        return UNREAD_HOLD


# What `_held` gives when it could not read whether a label holds an issue.
UNREAD_HOLD = "a label it could not read"


def _held_line(issue, label, held, later):
    if label == UNREAD_HOLD:
        return (
            f"NexKit could not read whether a label holds #{issue}, so it {held}. The next "
            f"autopilot sweep {later} once it can read that none does."
        )
    return (
        f"#{issue} is on hold (`{label}`), so NexKit {held}. Once the label is removed, the "
        f"next autopilot sweep {later}."
    )


def _skipped_conflicting(gh, cfg, decision, pr, head, files, row, workflow_ref, default_branch):
    """(outcome, message) for a round whose commit conflicts with the base branch's tip as
    `publish` saw it: such a head cannot merge, so its checks and review did not run, and
    the conflict round starts at once, within `conflicts.max_rounds`, unless a label holds
    its issue."""
    base = decision["base"]
    listed = ", ".join(f"`{name}`" for name in files)
    message = (
        f"`{head[:7]}` conflicts with `{base}` in {listed}, so NexKit skipped its checks and "
        f"AI review: the pull request cannot merge until the conflict is resolved."
    )
    label = _held(gh, cfg, decision["issue"])
    if label:
        row["held_back"] = "conflicts"
        later = "starts the round that resolves it"
        held = "starts no round to resolve it"
        return autopilot.HELD, f"{message} {_held_line(decision['issue'], label, held, later)}"
    try:
        started = start_conflict_round(
            gh,
            pr,
            base,
            workflow_file(workflow_ref),
            default_branch,
            cfg["conflicts"]["max_rounds"],
            row=row,
        )
    except GitHubError as exc:
        started = f"NexKit could not start a round to merge `{base}`: {exc}"
    if started.startswith("Started"):
        return "conflict_fix", f"{message} {started}"
    started = started or f"No round to merge `{base}` started."
    return "conflicting", f"{message} {started} Comment `/nexkit fix` to resolve it."


# Outcomes of an implement round that leave its pull request as it is, with no round on
# its way: a conflict round skipped while it ran starts when it ends.
IMPLEMENT_LEFT = {"ready", "needs_human", "review_failed", "retry_failed", autopilot.CI_WAITING}


def _conflicts_after(gh, cfg, decision, pr, row, workflow, default_branch):
    """Start the conflict round that NexKit left out while this implement round ran, when
    the pull request conflicts with its base branch now and no label holds its issue.
    Returns text for the round."""
    if _held(gh, cfg, decision["issue"]):
        return ""
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
    """Try the merge again for a pull request whose round approved it but did not merge it:
    from an autopilot sweep, once required checks that had not passed may have
    (`merge_waiting`), or once GitHub reports a refused one as mergeable (`clean`); or
    after a writer approved it (see `maintain.merge_approved`), whose `intro` then starts
    the text. A required check that failed meanwhile gets an automatic fix round (see
    `_required_failed`). The round's comment shows what happened, unless the pull request
    was merged or closed first. Returns a line for the log."""
    pr = step["pull"]
    for comment, run in run_comments(gh.comments(pr)):
        if comment["id"] != step["round_comment"]:
            continue
        waited = run.get("outcome") == "merge_waiting"
        target = {"pr": pr, "head": run["head"], "issue": step["number"], "base": step["base"]}
        target["waiting"] = waited
        intro = step.get("intro", "")
        outcome, text = merge_pull(gh, cfg, target, run, intro, workflow, default_branch)
        if outcome == "merge_waiting" and waited:
            return f"#{pr} still waits for its required checks."
        if outcome == autopilot.PULL_CLOSED:
            # Its round stays as it was, as after any merge by a person, and NexKit's own
            # merge that got there first keeps its text.
            return f"#{pr}: pull_closed; its round stays as it was."
        if outcome == "merge_refused" and step.get("clean"):
            # GitHub called it mergeable and still refused: sweeps leave this head alone.
            run["refused_clean"] = run["head"]
        run.update(outcome=outcome, status=run_status(outcome))
        reason = needs_person(outcome, cfg, found={"pull": True, **target.get("found", {})})
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

    GitHub closes it too, through the pull request's `Closes` line, but not at once and not
    when a person edited that line away. Returns (closed, lines for the round's comment). A
    failure leaves the merge as it is."""
    try:
        if gh.issue(issue).get("state") != "open":
            return False, []
        commit = f" in {sha}" if sha else ""
        gh.comment(issue, f"NexKit merged #{pr}{commit}, which implements this issue.")
        gh.close_issue(issue, "completed")
    except GitHubError as exc:
        return False, [f"Could not close #{issue}: {exc}. Close it yourself."]
    return True, [f"Closed #{issue} as completed."]


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
        if to_decide(review):
            state["last_review"]["decide"] = [clip(i, 300) for i in to_decide(review)]


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
        "next",
        "no_change",
        "protected_paths",
    )
    for key in kept:
        if result.get(key):
            fields[key] = result[key]
    progress.finish(gh, decision, run_status(result["outcome"]), result["summary"], **fields)
    retry = result["outcome"] == autopilot.RETRY_WAITING
    wait_for_reset(gh, cfg, result.get("resume_at"), workflow_ref, default_branch, retry=retry)
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


def wait_for_reset(gh, cfg, at, workflow_ref, default_branch, retry=False):
    """Start the run that waits for a paused command's reset when it comes within
    `usage_limit.wait_minutes`, or, with `retry`, for a command's run again after a
    transient error, which comes within the longest wait of `retry.wait_minutes`. Later
    resumes are left to the scheduled check."""
    when = parse_iso(at)
    wait = reach(cfg, retry)
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
    carried = {
        "verdict": "approve",
        "summary": f"The review of `{sha[:7]}` carried over.",
        "criteria": [],
        "findings": findings,
        "carried": sha,
    }
    decide = (last.get("decide") or []) if last.get("head") == sha else []
    if decide:
        # The items a person decides wait for their approval of the new head too.
        carried["decide"] = decide
    return carried


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


def shortened_line(*results):
    """What the round's comment says about the texts that an agent's prompt kept only the
    start of, from the results of the round's agents, or "". A person who answers the
    agent then knows that NexKit shortened them, and that the agent had each whole."""
    found = {}
    for result in results:
        for item in (result or {}).get("shortened") or []:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                found.setdefault(clip(one_line(item["name"]), 200), item)
    if not found:
        return ""
    parts = []
    for name, item in list(found.items())[:10]:
        kept, length = item.get("kept"), item.get("length")
        sizes = isinstance(kept, int) and isinstance(length, int)
        parts.append(name + (f" ({kept:,} of {length:,} characters)" if sizes else ""))
    more = f", and {len(found) - 10} more" if len(found) > 10 else ""
    return (
        "**Shortened for the agent:** these texts are longer than a prompt keeps, so its "
        "prompt had only their start and the agent read each whole from a file: "
        + "; ".join(parts)
        + more
        + "."
    )


def _report(gh, decision, cfg, needs, artifacts, workflow_ref, default_branch):
    action = decision["action"]
    agent = _load(artifacts / "nexkit-agent" / "result.json")
    shortened = shortened_line(agent, _load(artifacts / "nexkit-review" / "result.json"))
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
            again = error and _retry(
                gh, cfg, decision, command, decision["target"], workflow_ref, default_branch
            )
            if again:
                outcome, message, fields = _retrying(
                    gh, cfg, decision, agent, error, again, message
                )
            elif error and not _retries_left(cfg, decision):
                outcome, message = "retry_failed", _retried(gh, decision, error, message)
            found = {
                "pull": bool(decision.get("pr")),
                "questions": round_questions(agent),
                "timed_out": bool((agent or {}).get("timed_out")),
                "error": error if outcome == "retry_failed" else _failure_error(agent, publish),
                "stops": int(decision.get("retry") or 0) + 1,
            }
            reason = needs_person(outcome, cfg, found=found)
            if agent and agent.get("unchanged"):
                reason = _with_finding(reason, agent)
            protected = protected_paths(agent)
            if outcome == "agent_blocked" and protected:
                reason = autopilot.protected_reason(protected)
            if reason and autopilot.carries_on(gh, cfg, decision, outcome, agent):
                reason = None
                message += "\n\nNexKit autopilot carries on from here."
            if shortened:
                message = f"{message}\n\n{shortened}"
            if decision.get("pr"):
                _stop_round(gh, decision, agent, outcome, message, reason, **fields)
            return {
                "outcome": outcome,
                "summary": message,
                "attention": reason,
                "agents": agents_ran(agent),
                "resume_at": (fields.get("resume") or {}).get("at"),
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
            if shortened:
                summary = f"{summary}\n\n{shortened}"
            return {
                "outcome": "planned",
                "summary": summary.strip(),
                "attention": reason,
                "agents": agents_ran(agent),
            }

    pr = publish.get("pr") or decision["pr"]
    head = publish.get("head") or decision["head"]
    recheck = action == "fix" and bool(publish.get("recheck"))
    # The round's commit conflicts with the base branch's tip: no checks or review ran.
    conflicting = action in ("implement", "fix") and bool(publish.get("conflicts"))
    # With the checks that route skipped, which ran on no runner.
    skipped = decision.get("skipped_checks")
    checks = with_skipped(cfg, _load(artifacts / "nexkit-checks" / "checks.json"), skipped)
    # A person's pull request whose configuration NexKit would refuse fails its checks.
    config_error = decision.get("config_error") or ""
    if config_error:
        checks = [config_check(config_error), *(checks or [])]
    reviewed = _load(artifacts / "nexkit-review" / "result.json")
    given = reviewed["output"] if reviewed and reviewed.get("status") == "done" else None
    review = apply_criteria(given) if given else None
    confirm = unverified(review)
    review_paused = bool(reviewed) and reviewed.get("status") == "paused"
    url = run_url()

    comments = gh.comments(pr)
    comment_id, state = read_state(comments)
    state = state or empty_state(decision["issue"])
    person = bool(decision.get("person"))
    if person:
        state["person"] = True
    # Only the review this round's decision named carries over.
    carried = (
        bool(reviewed)
        and reviewed.get("status") == "carried"
        and bool(decision.get("carry_from"))
        and reviewed.get("carried") == decision["carry_from"]
    )
    if carried:
        review = carried_over(state, reviewed)
    # Checklist items not met that a person decides: their approval merges.
    decide = to_decide(review)
    carry = _load(artifacts / "nexkit-review" / "carry.json") or {}
    # A round that waited for the default branch is over once another round reports.
    state.pop("ci_wait", None)
    round_id, row = progress.find_round(comments, decision)
    if decision.get("head") and decision["head"] != head:
        _restore_statuses(gh, decision["head"], state.get("feedback"), url)
    resume = None
    if review_paused:
        # The commit is published and checked; only the review runs again after the reset.
        if action == "review":
            again = auto_input(decision, resume=True)
        else:
            # The review stage of `/nexkit go` or a fix round resumes as `/nexkit review`.
            kept = {"autopilot": decision.get("autopilot"), "retry": decision.get("retry")}
            again = auto_input(kept, resume=True)
        message, resume = pause(
            reviewed, cfg, "review", pr, auto=again, resumed=bool(decision.get("resumed"))
        )
        _pause_statuses(gh, head, resume, url)
        if config_error:
            gh.set_status(head, "nexkit/checks", "failure", config_status(config_error), url)
        checks_ok, verdict = checks is not None and all(c["passed"] for c in checks), None
    elif conflicting:
        skipped = f"Not run: {head[:7]} conflicts with {decision['base']}"
        for context in progress.STATUS_CONTEXTS:
            gh.set_status(head, context, "error", skipped, url)
        checks_ok, verdict = False, None
    else:
        failed = "" if review else review_failure(reviewed)[0]
        checks_ok, verdict = _set_statuses(gh, head, checks, review, url, failed, config_error)
    if given:
        gh.create_review(pr, head, review_body({"output": given}, checks, person))

    row.update(
        head=head,
        checks="not run" if checks is None else ("passed" if checks_ok else "failed"),
        verdict=verdict or "not run",
        tokens=_tokens(agent if action != "review" else None, reviewed),
    )
    # The checks that passed on this head, also when others failed, for the next rounds'
    # NEXKIT_CHECK_VERIFIED_SHA.
    passed = {} if conflicting else passed_checks(checks)
    if passed:
        row["passed"] = passed
    else:
        row.pop("passed", None)
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

    found = {}
    if conflicting:
        outcome, message = _skipped_conflicting(
            gh, cfg, decision, pr, head, publish["conflicts"], row, workflow_ref, default_branch
        )
    elif review_paused:
        outcome = "paused"
        row["verdict"] = "paused"
        if resume:
            row["resume"] = resume
    elif person and verdict:
        # A person's pull request: nothing here pushes, starts a fix round or merges.
        outcome = "person_reviewed"
        if checks_ok and verdict == "approve" and not decide:
            message = f"✅ Checks passed and the AI review approved `{head[:7]}`."
        else:
            message = f"Checks or review need attention on `{head[:7]}`."
        message += f" {PERSON_LINE}"
        if decide:
            items = "\n".join(f"- {item}" for item in decide)
            message += (
                "\n\nOnly a person decides these checklist items, which the review found not "
                f"met:\n\n{items}"
            )
    elif checks_ok and verdict == "approve":
        outcome = "ready"
        approved = f"the AI review approved `{head[:7]}`"
        if carried:
            approved = f"the AI review's approval carried over to `{head[:7]}`"
        message = (
            f"✅ Checks passed and {approved}. This pull request is ready for a human decision."
        )
        if confirm or decide:
            message += _for_person(cfg, review, confirm, decide)
            if decide:
                # A person's approval of this head accepts them (`maintain.merge_approved`).
                row["decide"] = decide
        elif cfg["merge"]["auto"] and (label := _held(gh, cfg, decision["issue"])):
            outcome, row["held_back"] = autopilot.HELD, "merge"
            message += " " + _held_line(decision["issue"], label, "does not merge it", "merges it")
        elif cfg["merge"]["auto"]:
            target = {"pr": pr, "head": head, "issue": decision["issue"], "base": decision["base"]}
            # A required check that failed gets its fix round through this round's state.
            target.update(state=state, state_comment=comment_id)
            outcome, message = merge_pull(
                gh, cfg, target, row, message, workflow_file(workflow_ref), default_branch
            )
            comment_id = target["state_comment"]
            found = target.get("found", {})
    elif verdict is None:
        outcome = "review_failed"
        found["error"], message = review_failure(reviewed)
        error = _transient(decision, reviewed, {}, needs, "review")
        again = error and _retry(gh, cfg, decision, "review", pr, workflow_ref, default_branch)
        if again:
            outcome, message, fields = _retrying(gh, cfg, decision, reviewed, error, again, message)
            row.update(fields)
            resume = fields.get("resume") or resume
            if resume:
                # The review runs again later; until then it is neither passed nor failed.
                waits = f"Runs again after {shown(resume['at'])} after a transient error"
                gh.set_status(head, "nexkit/review", "pending", waits[:STATUS_LIMIT], url)
        elif error and not _retries_left(cfg, decision):
            outcome, message = "retry_failed", _retried(gh, decision, error, message)
            found.update(error=error, stops=int(decision.get("retry") or 0) + 1)
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
    elif state["auto_fixes"] < cfg["fix"]["max_auto_rounds"] and (
        label := _held(gh, cfg, decision["issue"])
    ):
        outcome, row["held_back"] = autopilot.HELD, "fix"
        message = _held_line(
            decision["issue"], label, "starts no automatic fix round", "starts the fix round"
        )
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
    note = str((agent or {}).get("no_change_note") or "").strip()
    if action == "implement" and note:
        message = f"{NO_CHANGE_NOTE}{clip(note, 1500)}\n\n{NO_CHANGE_KEPT}\n\n{message}"
    if action == "fix" and agent and agent.get("start_base"):
        files = ", ".join(f"`{name}`" for name in agent.get("conflicts") or [])
        merged = f"Merged `{decision['base']}` (`{agent['start_base'][:7]}`) into the branch"
        done = f" and resolved conflicts in {files}." if files else ", which was behind it."
        message = f"{merged}{done}\n\n{message}"
    if publish.get("updated"):
        message = (
            f"Merged `{decision['base']}` (`{publish['updated'][:7]}`) after this round's "
            "commit: its rules want pull requests up to date, so the checks ran on a head that "
            f"contains it.\n\n{message}"
        )
    if checks is not None and skipped_names(skipped):
        lines = [skipped_line(name, since) for name, since in skipped_names(skipped).items()]
        message = "\n".join(lines) + f"\n\n{message}"
    carry_line = _carry_line(decision, decision["base"], carry)
    if carry_line:
        message = f"{carry_line}\n\n{message}"
    if config_error:
        message = f"{config_line(config_error, head)}\n\n{message}"
    if action == "implement" and outcome in IMPLEMENT_LEFT and cfg["conflicts"]["auto_resolve"]:
        message += _conflicts_after(
            gh, cfg, decision, pr, row, workflow_file(workflow_ref), default_branch
        )
    if shortened:
        message = f"{message}\n\n{shortened}"
    row.update(status=run_status(outcome), outcome=outcome)
    failing = [c["name"] for c in checks or [] if not c.get("passed")]
    blocking = [f for f in (review or {}).get("findings") or [] if f.get("severity") == "blocking"]
    found = {"pull": True, "failing": failing, "findings": blocking, **found}
    reason = needs_person(outcome, cfg, resume=resume, confirm=confirm, decide=decide, found=found)
    if outcome == "recheck_failed":
        reason = _with_finding(reason, agent)
    if reason:
        row["attention"] = reason
    fix = {"command": "fix", "number": str(pr), "note": "", "auto": "true"}
    if outcome == "auto_fix":
        # The round this one starts, so a sweep starts it once more when it never runs.
        row["next"] = fix
    progress.save_state(gh, pr, comment_id, state)
    progress.save_round(gh, pr, round_id, row, message)
    if outcome == "auto_fix":
        gh.dispatch(workflow_file(workflow_ref), default_branch, fix)
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
