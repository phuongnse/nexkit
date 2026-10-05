"""Report a finished pipeline run on GitHub and decide what happens next."""

from __future__ import annotations

import json
from pathlib import Path

from .github import GitHubError, run_url
from .state import empty_state, read_state, render_state

ICONS = {True: "✅", False: "❌"}


def _load(path):
    path = Path(path)
    return json.loads(path.read_text()) if path.is_file() else None


def _link():
    url = run_url()
    return f" [Run log]({url})." if url else ""


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


def report(gh, decision, cfg, needs, artifacts, *, workflow_ref, default_branch):
    """Post the outcome. Returns a short machine-readable summary for logs and tests."""
    action = decision["action"]
    artifacts = Path(artifacts)
    target = decision["target"]
    agent = _load(artifacts / "nexkit-agent" / "result.json")
    publish = json.loads(((needs.get("publish") or {}).get("outputs") or {}).get("result") or "{}")

    if action in ("plan", "implement", "fix"):
        if agent is None:
            gh.comment(target, f"NexKit could not run the {action} agent.{_link()}")
            return {"outcome": "agent_failed"}
        if agent["status"] != "done":
            cost = f" Cost: ${agent['cost']:.2f}." if agent.get("cost") else ""
            gh.comment(target, f"NexKit stopped during {action}: {agent['error']}{cost}{_link()}")
            return {"outcome": "agent_" + agent["status"]}
        if not publish.get("published"):
            reason = publish.get("error") or "the publish job failed"
            gh.comment(target, f"NexKit could not publish the {action} result: {reason}{_link()}")
            return {"outcome": "publish_failed"}
        if action == "plan":
            return {"outcome": "planned"}

    pr = publish.get("pr") or decision["pr"]
    head = publish.get("head") or decision["head"]
    checks = _load(artifacts / "nexkit-checks" / "checks.json")
    review = _load(artifacts / "nexkit-review" / "result.json")
    url = run_url()

    comments = gh.comments(pr)
    comment_id, state = read_state(comments)
    state = state or empty_state(decision["issue"])
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
    verdict = None
    if review and review.get("status") == "done":
        verdict = review["output"]["verdict"]
        gh.create_review(pr, head, review_body(review, checks))
        gh.set_status(
            head,
            "nexkit/review",
            "success" if verdict == "approve" else "failure",
            "AI review approved" if verdict == "approve" else "AI review requested changes",
            url,
        )
    else:
        gh.set_status(head, "nexkit/review", "error", "AI review did not complete", url)

    trigger = action + (" (auto)" if decision.get("auto") else "")
    state["rounds"].append(
        {
            "round": len(state["rounds"]) + 1,
            "trigger": trigger,
            "head": head,
            "checks": "not run" if checks is None else ("passed" if checks_ok else "failed"),
            "verdict": verdict or "not run",
            "cost": _cost(agent if action != "review" else None, review),
        }
    )
    state["feedback"] = {
        "head": head,
        "checks": checks or [],
        "review": review["output"] if verdict else None,
    }

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

    body = render_state(state)
    if comment_id:
        gh.update_comment(comment_id, body)
    else:
        gh.comment(pr, body)
    gh.comment(pr, message + _link())
    if outcome == "auto_fix":
        gh.dispatch(
            workflow_file(workflow_ref),
            default_branch,
            {"command": "fix", "number": str(pr), "note": "", "auto": "true"},
        )
    return {"outcome": outcome, "pr": pr, "head": head}
