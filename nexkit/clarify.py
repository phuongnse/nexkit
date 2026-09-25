"""Bounded requirement clarification on Actions, before human approval."""

from __future__ import annotations

import argparse
import os
from datetime import datetime

from .cli import SPEC_MARKER, set_spec
from .common import Blocked, canonical, digest, read_json, write_json
from .policy import agent_result, approval, config, control_command, human, now, require, spec_hash


def discussion(gh, number):
    # Only authorized human answers can decide product scope. Other issue text
    # remains untrusted context and cannot consume model calls through comments.
    return [
        {
            "id": c["id"],
            "body": c["body"],
            "updated_at": c["updated_at"],
            "actor": c["user"]["login"],
        }
        for c in gh.comments(number)
        if human(c, gh.permission) and not c.get("body", "").strip().startswith("/nexkit ")
    ]


def unapproved(gh, number):
    issue = gh.issue(number)
    require(issue.get("state") == "open", "Work item is closed")
    require(SPEC_MARKER in (issue.get("body") or ""), "Not a NexKit request")
    comments = gh.comments(number)
    require(control_command(comments, gh.permission) != "cancel", "Requirement work cancelled")
    try:
        approval(issue, comments, gh.permission)
    except Blocked:
        return issue
    raise Blocked("Requirement is approved; clarification cannot edit it")


def prepare(gh, number, run_key, kit_ref, event):
    if event.get("comment"):
        comment = event["comment"]
        if not human(comment, gh.permission) or comment["body"].strip().startswith(
            "/nexkit approve "
        ):
            return {"ready": False, "reason": "Not an authorized requirement response"}
    issue = gh.issue(number)
    if SPEC_MARKER not in (issue.get("body") or "") or issue.get("pull_request"):
        return {"ready": False, "reason": "Not a NexKit request"}
    state, revision = gh.get_state(number)
    phase = state.get("clarification", {})
    try:
        issue = unapproved(gh, number)
        require(
            state.get("status") not in ("implementing", "verifying", "merged"),
            "Delivery has started",
        )
        repo = gh.repo()
        base = gh.ref(repo["default_branch"])
        cfg = config(gh.read_config(base))
        require(
            cfg["repository"] == gh.repository and cfg["default_branch"] == repo["default_branch"],
            "Project identity changed",
        )
        require(cfg["kit"]["ref"] == kit_ref, "Clarification kit pin changed")
        answers = discussion(gh, number)
        fingerprint = digest({"spec": spec_hash(issue), "answers": answers})
        if phase.get("completed_input") == fingerprint:
            if phase.get("message"):
                post_notice(gh, number, phase["message"])
            return {"ready": False, "reason": "No new requirement input"}
        require(phase.get("run_key") != run_key, "Duplicate clarification run")
        require(
            phase.get("calls", 0) < cfg["limits"]["attempts"],
            "Requirement clarification attempts exhausted",
        )
        require(
            state.get("agent_calls", 0) + 1 <= cfg["limits"]["agent_calls"],
            "Agent invocation budget exhausted",
        )
        minutes = max(1, min(15, cfg["limits"]["minutes"] // cfg["limits"]["attempts"]))
        require(
            phase.get("reserved_minutes", 0) + minutes <= cfg["limits"]["minutes"],
            "Requirement clarification time budget exhausted",
        )
        phase.update(
            status="clarifying",
            run_key=run_key,
            calls=phase.get("calls", 0) + 1,
            reserved_minutes=phase.get("reserved_minutes", 0) + minutes,
            started_at=now(),
            input=fingerprint,
        )
        state.update(clarification=phase, agent_calls=state.get("agent_calls", 0) + 1)
        gh.save_state(number, state, revision)
        return {
            "ready": True,
            "stage": "requirement",
            "repository": gh.repository,
            "issue": issue,
            "answers": answers,
            "pending_questions": phase.get("questions", []),
            "config": cfg,
            "base": base,
            "source": base,
            "run_key": run_key,
            "agent_minutes": minutes,
            "input": fingerprint,
        }
    except Blocked as exc:
        # Never disturb an approved delivery merely because a delayed comment
        # event also reached this workflow.
        phase.update(status="waiting", reason=str(exc))
        state["clarification"] = phase
        gh.save_state(number, state, revision)
        return {"ready": False, "reason": str(exc)}


def revalidate(gh, context):
    issue = unapproved(gh, context["issue"]["number"])
    require(
        spec_hash(issue) == spec_hash(context["issue"]), "Requirement changed during clarification"
    )
    require(
        discussion(gh, issue["number"]) == context["answers"], "New requirement answers arrived"
    )
    cfg = context["config"]
    require(
        gh.ref(cfg["default_branch"]) == context["base"], "Repository changed during clarification"
    )
    require(digest(gh.read_config(context["base"])) == digest(cfg), "Setup configuration changed")
    state, revision = gh.get_state(issue["number"])
    phase = state.get("clarification", {})
    require(
        phase.get("run_key") == context["run_key"] and phase.get("status") == "clarifying",
        "Superseded clarification run",
    )
    require(
        (
            datetime.fromisoformat(now()) - datetime.fromisoformat(phase["started_at"])
        ).total_seconds()
        < context["agent_minutes"] * 60 + 300,
        "Clarification run deadline exhausted",
    )
    return state, revision


def publish(gh, context, bundle):
    state, revision = revalidate(gh, context)
    require(
        bundle.get("run_key") == context["run_key"] and bundle.get("input") == context["input"],
        "Requirement output provenance mismatch",
    )
    require(bundle.get("unchanged") is True, "Requirement agent changed the source")
    result = agent_result(bundle.get("result"), "request")
    require(result["status"] == "done", "Requirement agent blocked: " + result["summary"])
    require(
        isinstance(result.get("specification"), str) and result["specification"].strip(),
        "Missing requirement specification",
    )
    questions = result.get("questions")
    require(
        isinstance(questions, list)
        and len(questions) <= 10
        and all(isinstance(q, str) and 0 < len(q) <= 1000 for q in questions),
        "Invalid requirement questions",
    )
    require(
        type(result.get("ready_for_approval")) is bool
        and result["ready_for_approval"] == (not questions),
        "Requirement readiness contradicts its questions",
    )
    prefix = context["issue"]["body"].split(SPEC_MARKER, 1)[0]
    require(
        len((prefix + SPEC_MARKER + result["specification"]).encode()) < 60000,
        "Requirement exceeds the issue size bound",
    )
    updated = set_spec(gh, context["issue"]["number"], result["specification"])
    current = gh.issue(context["issue"]["number"])
    # Record completion before the comment. On interruption the next run can
    # recover the missing comment without invoking another model.
    phase = state["clarification"]
    message = (
        "<!-- nexkit:clarification:"
        + digest({"spec": updated["spec"], "questions": questions})
        + " -->\n"
    )
    if questions:
        message += "Please answer these requirement questions on this issue:\n\n" + "\n".join(
            f"{i}. {q}" for i, q in enumerate(questions, 1)
        )
    else:
        message += (
            "The specification is ready for human review. An authorized human may approve this exact version with:\n\n`"
            + updated["human_approval_comment"]
            + "`\n"
        )
    phase.update(
        status="awaiting_answers" if questions else "awaiting_approval",
        questions=questions,
        completed_input=digest({"spec": spec_hash(current), "answers": context["answers"]}),
        message=message,
    )
    gh.save_state(current["number"], state, revision)
    post_notice(gh, current["number"], message)
    return {"issue": updated["issue"], "status": phase["status"]}


def post_notice(gh, number, message):
    marker = message.split("\n", 1)[0]
    if not any((c.get("body") or "").startswith(marker) for c in gh.comments(number)):
        gh.comment(number, message)


def main():
    from .ci import event_issue, output
    from .github import GitHub
    from .policy import agent_runner, authentication

    p = argparse.ArgumentParser()
    p.add_argument("operation", choices=("prepare", "guard", "publish", "failed"))
    p.add_argument("--context", default="/tmp/nexkit/context.json")
    p.add_argument("--result", default="/tmp/nexkit/requirement.json")
    p.add_argument("--kit-ref")
    args = p.parse_args()
    gh = GitHub(os.environ["GITHUB_REPOSITORY"])
    try:
        if args.operation == "prepare":
            require(
                os.environ["GITHUB_REF"] == "refs/heads/" + gh.repo()["default_branch"],
                "Requirement workflow must run on the default branch",
            )
            context = prepare(
                gh,
                event_issue(),
                os.environ["GITHUB_RUN_ID"] + "." + os.environ.get("GITHUB_RUN_ATTEMPT", "1"),
                args.kit_ref,
                read_json(os.environ["GITHUB_EVENT_PATH"]),
            )
            write_json(args.context, context)
            output(ready=context["ready"])
            if context["ready"]:
                cfg = context["config"]
                output(
                    base=context["base"],
                    model=cfg["models"]["implement"],
                    effort=cfg.get("reasoning_effort", {}).get("implement", ""),
                    codex_version=cfg["engine"]["version"],
                    agent_runner=canonical(agent_runner(cfg)),
                    authentication=authentication(cfg),
                    agent_minutes=context["agent_minutes"],
                )
            result = context
        else:
            context = read_json(args.context)
            if args.operation == "guard":
                revalidate(gh, context)
                result = {"authorized": True}
            elif args.operation == "publish":
                result = publish(gh, context, read_json(args.result))
            else:
                state, revision = gh.get_state(context["issue"]["number"])
                phase = state.get("clarification", {})
                if (
                    phase.get("run_key") == context["run_key"]
                    and phase.get("status") == "clarifying"
                ):
                    phase.update(
                        status="blocked",
                        reason="Requirement agent job failed; inspect Actions logs and resume",
                    )
                    gh.save_state(context["issue"]["number"], state, revision)
                result = phase
        print(canonical(result))
        return 0
    except Blocked as exc:
        print(f"NexKit requirement blocked: {exc}", file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
