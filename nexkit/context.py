"""Collect the GitHub material an agent stage needs, before the agent runs.

Gathering happens in its own step so the agent process never holds a GitHub token.
"""

from __future__ import annotations

import re
from pathlib import Path

from .checks import RESULTS_DIR, failing_signatures
from .config import NAME
from .evidence import collect, named_runs, summary_lines
from .github import TRUSTED_ASSOCIATIONS
from .redact import Redactor
from .route import parse_command
from .runlog import write_summary
from .state import answers_text, by_bot, clip, latest_plan, plan_text, read_state

MAX_COMMENTS = 30
NO_PLAN = "No plan was posted. Implement the issue as described."
# The section of a NexKit pull request description that `publish.pull_body` writes from the
# implement result's `outside_plan`.
OUTSIDE_PLAN = re.compile(r"^## Outside the plan\n(.*?)(?=^## |^---$|\Z)", re.M | re.S)


def _trusted(item):
    return item.get("author_association") in TRUSTED_ASSOCIATIONS and not by_bot(item)


def _discussion(comments, notes=False):
    """Collaborators' comments, and the answers autopilot gave for the owner. Command
    comments are left out, or with `notes` kept when they carry a note: the agent that acts
    on a command gets its note through the decision, the reviewer only through the
    discussion."""
    entries = []
    for comment in comments:
        answered = answers_text(comment)
        if answered is not None:
            # Autopilot answered for the owner, with the options NexKit recommended.
            entries.append(
                f"NexKit autopilot for the owner ({comment['created_at']}):\n{clip(answered)}"
            )
            continue
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


def _saved_feedback(comments):
    """The checks and review that NexKit last recorded on the pull request."""
    _, state = read_state(comments)
    return (state or {}).get("feedback") or {}


def _after_failed_checks(decision, comments):
    """Whether this is an automatic round started after failing checks, NexKit's own or
    required checks that failed when NexKit merged. It merges the base
    branch whenever the base has moved, so a failure that the base branch already fixed
    goes away without an agent change."""
    if not decision.get("auto") or decision.get("conflicts"):
        return False
    feedback = _saved_feedback(comments)
    if feedback.get("required"):
        return True
    return any(not check.get("passed") for check in feedback.get("checks") or [])


def _inside(path):
    """Whether a recorded result path stays inside the folder it is relative to."""
    return (
        isinstance(path, str)
        and bool(path)
        and not path.startswith(("/", "~"))
        and ".." not in path.split("/")
    )


def evidence(feedback, decision, folder):
    """The result files that the failing checks of the run in `results_run` kept, as the
    agent job downloaded them into `folder`, outside the working tree, or None. Says in
    the log why there are none."""
    run_id = decision.get("results_run")
    if not run_id:
        return None
    root = Path(folder).resolve() / RESULTS_DIR if folder else None
    if not root or not root.is_dir():
        print(
            "No result files of the failing checks: the `nexkit-checks` artifact of run "
            f"{run_id} could not be downloaded, for example because it expired. The fix "
            "prompt has no section about them.",
            flush=True,
        )
        return None
    found = []
    for check in feedback.get("checks") or []:
        name = check.get("name")
        if check.get("passed") or not isinstance(name, str) or not NAME.match(name):
            continue
        paths = []
        for path in check.get("results") or []:
            target = root / name / path if _inside(path) else None
            if not target or not target.exists():
                continue
            files = sum(f.is_file() for f in target.rglob("*")) if target.is_dir() else 1
            paths.append({"path": path, "folder": target.is_dir(), "files": files})
        if paths:
            left_out = check.get("results_left_out")
            found.append(
                {
                    "name": name,
                    "run": str(check.get("run") or ""),
                    "folder": str(root / name),
                    "paths": paths,
                    "left_out": left_out if isinstance(left_out, int) else 0,
                }
            )
    if not found:
        print(f"Run {run_id} kept no result files of the failing checks.", flush=True)
        return None
    print(f"Result files of the failing checks are in {root}.", flush=True)
    return {"folder": str(root), "checks": found}


def _failing_text(fix):
    """Which checks still failed after a recorded fix round, or "" when unknown."""
    failing = fix.get("failing")
    if not isinstance(failing, list):
        return ""  # the checks did not run, or the round was recorded before NexKit kept them
    if not failing:
        return "Every check passed after it."
    items = [f"`{item.get('name')}` (`{clip(item.get('signature'), 200)}`)" for item in failing]
    return "Checks that still failed after it, with their first failure: " + ", ".join(items)


def earlier_fixes(comments):
    """The fix prompt's list of the pull request's earlier fix rounds, oldest first: who
    started each, its note, the fix agent's summary and cause, and the checks that still
    failed after it. "" before the first."""
    _, state = read_state(comments)
    lines = []
    for fix in (state or {}).get("fixes") or []:
        who = "automatic" if fix.get("auto") else "requested by a person"
        kind = ", a diagnosis round" if fix.get("diagnosis") else ""
        lines += [
            f"### Round {fix.get('round')}: fix ({who}{kind})",
            "",
            f"Note: {clip(fix.get('note'), 2000) or 'None.'}",
            "",
            "Summary from the fix agent:",
            "",
            clip(fix.get("summary")) or "None.",
            "",
        ]
        if fix.get("cause"):
            lines += ["Cause it found:", "", clip(fix["cause"], 2000), ""]
        failing = _failing_text(fix)
        if failing:
            lines += [failing, ""]
    return "\n".join(lines).strip()


def diagnosis(decision, feedback):
    """The checks that this automatic round diagnoses, each with the failure it repeated,
    or []. `route` named them in the decision."""
    names = decision.get("diagnose") or []
    signatures = failing_signatures(feedback.get("checks"))
    return [{"name": name, "signature": signatures[name]} for name in names if name in signatures]


def _required_text(item):
    """A required check that failed, so GitHub refused the merge, after NexKit's own
    checks and review passed: its job's failed commands and the tests it names."""
    name = item.get("name") or "a required check"
    lines = [f"### Required check `{name}` failed, so GitHub refused the merge", ""]
    lines.append(
        "NexKit's own checks and review passed on this commit, but this check of the "
        "repository's CI, which branch protection requires, failed."
    )
    if item.get("url"):
        lines.append(f"Its job: {item['url']}")
    commands = [f"`{clip(command, 300)}`" for command in item.get("commands") or []]
    if commands:
        lines.append(f"Commands that failed in it: {', '.join(commands)}.")
    tests = [f"`{clip(test, 300)}`" for test in (item.get("tests") or [])[:30]]
    if tests:
        lines.append(f"Tests its annotations name: {', '.join(tests)}.")
    return "\n".join(lines)


def _feedback(gh, decision, comments):
    """Earlier NexKit results plus current human review feedback on the pull request."""
    pr = decision["pr"]
    head = decision["head"]
    parts = []
    saved = _saved_feedback(comments)
    for check in saved.get("checks") or []:
        if not check.get("passed"):
            parts.append(
                f"### Failing check `{check['name']}` (`{check['run']}`)\n\n"
                f"Exit code {check.get('exit_code')}. End of output:\n\n```\n"
                f"{clip(check.get('output'), 6000)}\n```"
            )
    for item in saved.get("required") or []:
        parts.append(_required_text(item))
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
                "cause": (fix_result.get("output") or {}).get("cause"),
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
        if str(fix.get("cause") or "").strip():
            lines += ["", "Cause the fix agent found:", "", clip(fix["cause"], 2000)]
        if fix.get("conflicts"):
            files = ", ".join(f"`{name}`" for name in fix["conflicts"])
            lines += ["", f"This round merged the base branch and resolved conflicts in {files}."]
        elif fix.get("conflicts") == []:
            lines += ["", "This round merged the base branch, which merged without conflicts."]
    return {"previous_head": last["head"], "previous_round": "\n".join(lines)}


def outside_plan(pull):
    """What the implementation agent did outside the plan, and why, from the pull request
    description that NexKit wrote."""
    body = ((pull or {}).get("body") or "").replace("\r\n", "\n")
    match = OUTSIDE_PLAN.search(body)
    return clip(match.group(1)) if match and match.group(1).strip() else "None."


def _fixed(decision, fix_result):
    return decision["action"] == "fix" and (fix_result or {}).get("status") == "done"


def ci_evidence(gh, cfg, issue, comments, folder, workflow=""):
    """Download the evidence of the CI runs the issue names into `folder`, before the agent
    runs, and say in the log and the run summary what was kept, missing or skipped. Runs of
    NexKit's own workflow, the file `workflow`, are skipped. None when the issue names no
    run."""
    runs, skipped = named_runs(gh, issue, comments)
    if not runs and not skipped:
        return None
    redact = Redactor()
    try:
        found = collect(gh, cfg, runs, folder, redact, workflow)
        found["skipped"] = [*skipped, *found["skipped"]]
    except Exception as exc:  # noqa: BLE001 - the round runs without the evidence
        print(redact(f"Could not collect the evidence of CI runs: {exc}"), flush=True)
        return None
    lines = [*summary_lines(found), *(f"Not collected: {reason}." for reason in found["skipped"])]
    for line in lines:
        print(redact(line), flush=True)
    write_summary("\n".join(["### NexKit: evidence from CI runs", "", *lines]) + "\n", redact)
    return found


def gather(gh, decision, stage=None, fix_result=None, results=None, ci=None, cfg=None, workflow=""):
    """Context for an agent stage. `stage` defaults to the decision's action; the review
    stage also gets the previous review round, with `fix_result` from this run's fix. A
    fix round also gets the failing checks' result files, which the agent job downloaded
    into the folder `results`. With `ci` and `cfg`, an implement or fix round also gets
    the evidence of the CI runs the issue names, downloaded into the folder `ci`, other
    than runs of NexKit's own workflow, the file `workflow`."""
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
    if stage in ("implement", "fix") and ci and cfg:
        context["ci_evidence"] = ci_evidence(gh, cfg, issue, comments, ci, workflow)
    if stage == "plan":
        # Triage reads the notes on earlier commands too: a later note can change the
        # profile that an earlier comment asked for.
        context["requests"] = _discussion(comments, notes=True)
        if plan:
            # A re-plan revises the latest plan with what was said since it.
            at = comments.index(plan)
            context["discussion"] = _discussion(comments[:at])
            context["since_plan"] = _discussion(comments[at + 1 :], notes=True)
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
        pr_comments = gh.comments(decision["pr"])
        context["feedback"] = _feedback(gh, decision, pr_comments)
        context["merge_moved_base"] = _after_failed_checks(decision, pr_comments)
        saved = _saved_feedback(pr_comments)
        context["evidence"] = evidence(saved, decision, results)
        context["earlier_fixes"] = earlier_fixes(pr_comments)
        context["diagnose"] = diagnosis(decision, saved)
        context["discussion"] += "\n\n---\n\nOn the pull request:\n\n" + _discussion(pr_comments)
    return context
