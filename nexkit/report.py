"""Report a finished pipeline run on GitHub and decide what happens next."""

from __future__ import annotations

import json
from pathlib import Path

from . import progress
from .github import GitHubError, run_url
from .state import FAILURE, SUCCESS, clip, empty_state, read_state

ICONS = {True: "✅", False: "❌"}
RESOLUTIONS = {
    "resolved": "✅ resolved",
    "unresolved": "🛑 unresolved",
    "rejection_accepted": "🤝 rejection accepted",
}
# Outcomes in which the run did its work. The others stopped on an error.
COMPLETED = {"planned", "ready", "merged", "auto_fix", "needs_human"}
# Bounds for what the state comment keeps for the next review.
MAX_FINDINGS = 30
MAX_FIXES = 5


def _load(path):
    path = Path(path)
    return json.loads(path.read_text()) if path.is_file() else None


def _cost(*results):
    costs = [r.get("cost") for r in results if r and isinstance(r.get("cost"), (int, float))]
    return round(sum(costs), 4) if costs else None


def review_body(review, checks):
    output = review["output"]
    approved = output["verdict"] == "approve"
    lines = [
        f"### NexKit review: {'approve' if approved else 'changes requested'}",
        "",
        output["summary"].strip(),
    ]
    if output.get("previous_findings"):
        lines += ["", "**Previous findings**", ""]
        for item in output["previous_findings"]:
            mark = RESOLUTIONS.get(item["resolution"], item["resolution"])
            lines.append(f"- {mark}: {item['finding']}: {item['evidence']}")
    if output.get("criteria"):
        lines += ["", "**Acceptance criteria**", ""]
        for item in output["criteria"]:
            lines.append(f"- {ICONS[bool(item['met'])]} {item['criterion']}: {item['evidence']}")
    if output.get("findings"):
        lines += ["", "**Findings**", ""]
        for item in output["findings"]:
            mark = "🛑 blocking" if item["severity"] == "blocking" else "💡 suggestion"
            where = f" `{item['file']}:{item['line']}`" if item.get("file") else ""
            lines.append(f"- {mark}{where}: {item['body']}")
    if checks:
        lines += [
            "",
            "**Checks**: " + ", ".join(f"{ICONS[c['passed']]} `{c['name']}`" for c in checks),
        ]
    return "\n".join(lines)


def workflow_file(workflow_ref):
    """`owner/repo/.github/workflows/nexkit.yml@refs/heads/main` -> `nexkit.yml`."""
    return workflow_ref.split("@", 1)[0].rsplit("/", 1)[-1]


def _set_statuses(gh, head, checks, review, url):
    """Set both commit statuses from check results and a review output (None: not run)."""
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
        gh.set_status(
            head,
            "nexkit/review",
            "success" if verdict == "approve" else "failure",
            "AI review approved" if verdict == "approve" else "AI review requested changes",
            url,
        )
    else:
        gh.set_status(head, "nexkit/review", "error", "AI review did not complete", url)
    return checks_ok, verdict


def _restore_statuses(gh, sha, feedback, url):
    """Replace the pending statuses `route` set on a commit this run did not check."""
    feedback = feedback or {}
    if feedback.get("head") == sha:
        _set_statuses(gh, sha, feedback.get("checks"), feedback.get("review"), url)
        return
    for context in progress.STATUS_CONTEXTS:
        gh.set_status(sha, context, "error", "The NexKit round did not finish", url)


def _stage_failure(action, agent, publish):
    """(outcome, message) when the agent or publish stage stopped, otherwise None."""
    if agent is None:
        return "agent_failed", f"NexKit could not run the {action} agent."
    if agent["status"] != "done":
        cost = f" Cost: ${agent['cost']:.2f}." if agent.get("cost") else ""
        return "agent_" + agent["status"], f"NexKit stopped during {action}: {agent['error']}{cost}"
    if not publish.get("published"):
        reason = publish.get("error") or "the publish job failed"
        return "publish_failed", f"NexKit could not publish the {action} result: {reason}"
    return None


def _stop_round(gh, decision, agent, message):
    """Mark a pull request round that published nothing as failed, with the reason."""
    comments = gh.comments(decision["pr"])
    _, state = read_state(comments)
    _restore_statuses(gh, decision["head"], (state or {}).get("feedback"), run_url())
    comment_id, row = progress.find_round(comments, decision)
    row.update(status=FAILURE, head=decision["head"], cost=_cost(agent))
    progress.save_round(gh, decision["pr"], comment_id, row, message)


def _record(state, decision, row, agent, review):
    """Keep what the next review needs: the last review's findings and the fixes since."""
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


def report(gh, decision, cfg, needs, artifacts, *, workflow_ref, default_branch):
    """Post the outcome. Returns a short machine-readable summary for logs and tests."""
    result = _report(gh, decision, cfg, needs, Path(artifacts), workflow_ref, default_branch)
    progress.finish(gh, decision, result["outcome"] in COMPLETED, result["summary"])
    return result


def _report(gh, decision, cfg, needs, artifacts, workflow_ref, default_branch):
    action = decision["action"]
    agent = _load(artifacts / "nexkit-agent" / "result.json")
    publish = json.loads(((needs.get("publish") or {}).get("outputs") or {}).get("result") or "{}")

    if action in ("plan", "implement", "fix"):
        failure = _stage_failure(action, agent, publish)
        if failure:
            outcome, message = failure
            if decision.get("pr"):
                _stop_round(gh, decision, agent, message)
            return {"outcome": outcome, "summary": message}
        if action == "plan":
            plan = publish.get("comment")
            return {"outcome": "planned", "summary": f"[Plan]({plan}) posted." if plan else ""}

    pr = publish.get("pr") or decision["pr"]
    head = publish.get("head") or decision["head"]
    checks = _load(artifacts / "nexkit-checks" / "checks.json")
    reviewed = _load(artifacts / "nexkit-review" / "result.json")
    review = reviewed["output"] if reviewed and reviewed.get("status") == "done" else None
    url = run_url()

    comments = gh.comments(pr)
    comment_id, state = read_state(comments)
    state = state or empty_state(decision["issue"])
    round_id, row = progress.find_round(comments, decision)
    if decision.get("head") and decision["head"] != head:
        _restore_statuses(gh, decision["head"], state.get("feedback"), url)
    checks_ok, verdict = _set_statuses(gh, head, checks, review, url)
    if review:
        gh.create_review(pr, head, review_body({"output": review}, checks))

    row.update(
        head=head,
        checks="not run" if checks is None else ("passed" if checks_ok else "failed"),
        verdict=verdict or "not run",
        cost=_cost(agent if action != "review" else None, reviewed),
    )
    _record(state, decision, row, agent or {}, review)
    state["feedback"] = {"head": head, "checks": checks, "review": review}

    if checks_ok and verdict == "approve":
        outcome = "ready"
        message = (
            f"✅ Checks passed and the AI review approved `{head[:7]}`. "
            "This pull request is ready for a human decision."
        )
        if cfg["auto_merge"]:
            try:
                gh.merge(pr, head)
                outcome, message = "merged", f"✅ Checks and AI review passed; merged `{head[:7]}`."
            except GitHubError as exc:
                message += f" Automatic merge was not possible: {exc}"
    elif verdict is None:
        outcome = "review_failed"
        message = "The AI review did not complete. Comment `/nexkit review` to try again."
    elif state["auto_fixes"] < cfg["max_auto_fixes"]:
        state["auto_fixes"] += 1
        outcome = "auto_fix"
        message = (
            f"Starting automatic fix round {state['auto_fixes']} of {cfg['max_auto_fixes']} "
            "for the failing checks and blocking findings."
        )
    else:
        outcome = "needs_human"
        message = (
            "Checks or review still need attention, and no automatic fix rounds remain. "
            "Comment `/nexkit fix <instructions>`, push a commit yourself, or close the PR."
        )

    row["status"] = SUCCESS if outcome in COMPLETED else FAILURE
    progress.save_state(gh, pr, comment_id, state)
    progress.save_round(gh, pr, round_id, row, message)
    if outcome == "auto_fix":
        gh.dispatch(
            workflow_file(workflow_ref),
            default_branch,
            {"command": "fix", "number": str(pr), "note": "", "auto": "true"},
        )
    summary = f"Pull request #{pr}: {message}" if action == "implement" else message
    return {"outcome": outcome, "pr": pr, "head": head, "summary": summary}
