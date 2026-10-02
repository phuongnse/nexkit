"""GitHub's FIFO workflow concurrency serializes work item creation and adoption."""

import json
import os
import re
import sys

from .cli import (
    MANUAL_SPEC_NOTICE,
    SPEC_MARKER,
    create_request,
    request_body,
    requirement_approval_text,
    set_spec,
    specification_body,
)
from .common import Blocked, canonical, read_json
from .github import GitHub
from .pipelines import dispatch, issue_pipeline, load_run_config, pipeline_id
from .policy import (
    HASH,
    HUMAN_PERMISSIONS,
    approval,
    control_command,
    human,
    require,
    spec_hash,
    start_command,
)
from .release import candidate


def live_start_comment(gh, event):
    """Recheck the exact native command and current collaborator permission."""
    original = event["comment"]
    current = gh.api(f"{gh.root}/issues/comments/{int(original['id'])}")
    require(
        current.get("issue_url")
        == f"https://api.github.com/{gh.root}/issues/{int(event['issue']['number'])}",
        "Start comment belongs to another issue",
    )
    require(
        current.get("body") == original.get("body")
        and current.get("created_at") == current.get("updated_at")
        and current.get("updated_at") == original.get("updated_at")
        and current.get("user", {}).get("id") == original.get("user", {}).get("id"),
        "Start comment changed; post a new command",
    )
    require(human(current, gh.permission), "Only an authorized collaborator can start an issue")
    return current


def issue_status(gh, issue):
    comments = gh.comments(issue["number"])
    require(
        control_command(comments, gh.permission) != "cancel",
        "Work is cancelled; use /nexkit resume",
    )
    state, _ = gh.get_state(issue["number"])
    try:
        approval(issue, comments, gh.permission)
    except Blocked as exc:
        if not str(exc).startswith("Awaiting an authorized approval comment:"):
            raise
        approved = False
    else:
        approved = True
    # An unbound issue may have a blocked record from an earlier delivery event.
    # Any real work, approval, or invocation reservation belongs to its old flow.
    started = bool(set(state) - {"status", "reason", "updated_at"}) or state.get("status") not in (
        None,
        "blocked",
    )
    return approved, started


def start_issue(gh, cfg, number, authorize):
    """Adopt in place under the existing serialized intake workflow."""
    require(type(number) is int and number > 0, "Use a positive GitHub issue number")
    raw = gh.api(f"{gh.root}/issues/{number}")
    require(not raw.get("pull_request"), "Start an issue, not a pull request")
    issue = gh.issue(number)
    require(issue.get("state") == "open", "Work item is closed")
    selected = pipeline_id(cfg)
    body = issue.get("body") or ""
    require(body.strip(), "Write the requirement in the issue body before starting intake")
    approved, started = issue_status(gh, issue)
    if body.startswith("<!-- nexkit:request:"):
        require(SPEC_MARKER in body, "Existing NexKit requirement metadata is incomplete")
        require(issue_pipeline(issue) == selected, "Work item belongs to another pipeline")
        return {
            "created": False,
            "adopted": False,
            "issue": issue,
            "clarify": not (approved or started),
        }
    require(
        "<!-- nexkit:" not in body,
        "Issue already contains NexKit metadata; inspect its existing workflow",
    )
    require(
        not approved and not started,
        "Existing approval or work must be continued through its current workflow",
    )
    updated = request_body(body, f"issue-{number}", pipeline=selected)

    authorize()
    current = gh.issue(number)
    require(current.get("state") == "open", "Work item is closed")
    require(
        spec_hash(current) == spec_hash(issue)
        and current.get("last_edited_at") == issue.get("last_edited_at"),
        "Issue changed during intake; post a new start command",
    )
    approved, started = issue_status(gh, current)
    require(not approved and not started, "Approval or work arrived during intake")
    gh.api(f"{gh.root}/issues/{number}", "PATCH", {"body": updated})
    current = gh.issue(number)
    require(
        spec_hash(current) == spec_hash({**issue, "body": updated}),
        "Issue changed after intake; inspect its requirement before continuing",
    )
    return {"created": False, "adopted": True, "issue": current, "clarify": True}


def bot_notice_exists(gh, number, prefix):
    """Only an actual Actions bot notice can satisfy intake acknowledgement."""
    return any(
        comment.get("user", {}).get("type") == "Bot"
        and comment.get("user", {}).get("login") == "github-actions[bot]"
        and (comment.get("body") or "").startswith(prefix)
        and (comment.get("body") or "")[len(prefix) :].strip()
        for comment in gh.comments(number)
    )


def publish_manual_spec(gh, cfg, payload, authorize):
    """The serialized Actions intake owns specification edits and bot notices."""
    require(
        set(payload) == {"issue", "body", "expected", "edited_at", "target"},
        "Invalid specification submission",
    )
    number = payload["issue"]
    require(type(number) is int and number > 0, "Use a positive GitHub issue number")
    require(
        all(
            isinstance(payload[key], str) and HASH.fullmatch(payload[key])
            for key in ("expected", "target")
        )
        and (payload["edited_at"] is None or isinstance(payload["edited_at"], str)),
        "Invalid specification revision",
    )
    issue = gh.issue(number)
    require(issue.get("state") == "open", "Work item is closed")
    require(
        not issue.get("pull_request"), "Publish a specification on an issue, not a pull request"
    )
    require(issue_pipeline(issue) == pipeline_id(cfg), "Work item belongs to another pipeline")
    target = spec_hash({**issue, "body": specification_body(issue, payload["body"])})
    require(
        target == payload["target"]
        and (
            spec_hash(issue) == target
            or (
                spec_hash(issue) == payload["expected"]
                and issue.get("last_edited_at") == payload["edited_at"]
            )
        ),
        "Requirement changed while publication was queued; read it and submit again",
    )

    def before_write(current):
        authorize()
        require(current.get("state") == "open", "Work item is closed")

    result = set_spec(gh, number, payload["body"], expected=issue, before_write=before_write)
    current = gh.issue(number)
    require(
        current.get("state") == "open" and spec_hash(current) == target,
        "Requirement changed before its approval notice",
    )
    message = (
        f"{MANUAL_SPEC_NOTICE}{target} -->\n"
        "The manually prepared specification has been published.\n\n"
        + requirement_approval_text(target)
    )
    if not any(
        comment.get("user", {}).get("type") == "Bot"
        and comment.get("user", {}).get("login") == "github-actions[bot]"
        and comment.get("body") == message
        for comment in gh.comments(number)
    ):
        authorize()
        gh.comment(number, message)
    return result


def start_notice(gh, cfg, result):
    number = result["issue"]["number"]
    selected = pipeline_id(cfg)
    marker = f"<!-- nexkit:start:{selected}:{number} -->"
    pipeline = f"pipeline `{selected}`" if selected else "the configured workflow"
    message = (
        f"{marker}\nThis issue is tracked by {pipeline}. Its existing discussion stays here.\n\n"
    )
    if bot_notice_exists(gh, number, message):
        return
    if not result["clarify"]:
        message += "Follow the existing requirement discussion and progress updates. Use `/nexkit resume` to recover interrupted work."
    elif result.get("clarification_dispatched"):
        message += (
            "NexKit will clarify the requirement on this issue and provide the exact approval "
            "command when it is ready. Implementation begins after requirement approval."
        )
    elif "clarify" not in cfg["binding"]["entrypoints"]:
        message += (
            f"This pipeline uses a manually prepared specification. Submit it with "
            f"`nexkit spec {number} --body-file requirement.md`. GitHub Actions will publish "
            "it and post the approval instructions here. Wait for that notice before "
            f"approving; `nexkit approval {number}` can then show the same command."
        )
    gh.comment(number, message)


def start_failure_notice(gh, event):
    """Report a failed authorized command without publishing internal errors."""
    if (
        os.environ.get("GITHUB_EVENT_NAME") != "issue_comment"
        or event.get("action") != "created"
        or event.get("issue", {}).get("pull_request")
        or not start_command(event.get("comment", {}).get("body"))
    ):
        return
    live_start_comment(gh, event)
    require(
        gh.permission(os.environ["GITHUB_ACTOR"]) in HUMAN_PERMISSIONS,
        "Intake permission was revoked",
    )
    run = os.environ.get("GITHUB_RUN_ID", "")
    require(run.isdigit(), "Native Actions run identity is required")
    marker = f"<!-- nexkit:start-error:{int(event['comment']['id'])} -->"
    number = event["issue"]["number"]
    prefix = f"{marker}\nNexKit intake needs attention. "
    if not bot_notice_exists(gh, number, prefix):
        gh.comment(
            number,
            prefix + "Review the "
            f"[Actions result](https://github.com/{gh.repository}/actions/runs/{run}) "
            "before retrying on this same issue.",
        )


def receive(gh, event):
    event_name = os.environ["GITHUB_EVENT_NAME"]
    bound = os.environ.get("NEXKIT_PIPELINE") or None
    if event_name == "issue_comment":
        if (
            event.get("action") != "created"
            or event.get("issue", {}).get("pull_request")
            or not start_command(event.get("comment", {}).get("body"))
        ):
            return {"skipped": True, "reason": "Not a new start command on an issue"}
        current = live_start_comment(gh, event)
        match = re.fullmatch(r"/nexkit start[ \t]+([a-z][a-z0-9_-]{0,47})", current["body"].strip())
        require(match is not None, "Use /nexkit start PIPELINE as a standalone comment")
        selected = match[1]
        if bound is not None and selected != bound:
            return {"skipped": True, "reason": "Start command selects another pipeline"}
        operation, payload = "start", {"issue": event["issue"]["number"]}
    else:
        require(event_name == "workflow_dispatch", "Intake accepts dispatches and start comments")
        inputs = event["inputs"]
        selected = bound or inputs.get("pipeline") or None
        require(
            not inputs.get("pipeline") or inputs["pipeline"] == selected,
            "Dispatched pipeline differs from the caller binding",
        )
        operation, payload = inputs["operation"], json.loads(inputs["payload"])
    require(
        gh.permission(os.environ["GITHUB_ACTOR"]) in HUMAN_PERMISSIONS,
        "Only an authorized project collaborator can submit work",
    )
    branch = gh.repo()["default_branch"]
    require(
        os.environ["GITHUB_REF"] == "refs/heads/" + branch, "Intake must run on the default branch"
    )
    base = gh.ref(branch)
    cfg = load_run_config(gh, base, selected, "intake")
    require(isinstance(payload, dict), "Invalid intake payload")

    def authorize():
        # Controls were verified at the immutable base; a default-branch or
        # revision change requires intake to use the newly accepted setup.
        require(
            gh.repo()["default_branch"] == branch and gh.ref(branch) == base,
            "Repository changed during intake; retry from the current branch",
        )
        require(
            gh.permission(os.environ["GITHUB_ACTOR"]) in HUMAN_PERMISSIONS,
            "Intake permission was revoked",
        )
        if event_name == "issue_comment":
            live_start_comment(gh, event)

    if operation == "request":
        require(set(payload) == {"title", "request", "key"}, "Invalid request fields")
        require(all(isinstance(v, str) and v for v in payload.values()), "Invalid request values")
        result = create_request(
            gh, payload["title"], payload["request"], payload["key"], pipeline=pipeline_id(cfg)
        )
    elif operation == "start":
        require(set(payload) == {"issue"}, "Start requires only the existing issue number")
        result = start_issue(gh, cfg, payload["issue"], authorize)
    elif operation == "spec":
        return publish_manual_spec(gh, cfg, payload, authorize)
    else:
        require(
            operation == "release" and set(payload) == {"commit", "version", "notes"},
            "Invalid release submission",
        )
        return candidate(gh, cfg, payload["commit"], payload["version"], payload["notes"])
    if result.get("clarify", True) and "clarify" in cfg["binding"]["entrypoints"]:
        if operation == "start":
            authorize()
        dispatch(gh, cfg, "clarify", {"issue": result["issue"]["number"]})
        result["clarification_dispatched"] = True
    manual_request = operation == "request" and "clarify" not in cfg["binding"]["entrypoints"]
    if manual_request and result["issue"].get("state") == "open":
        approved, started = issue_status(gh, result["issue"])
        result["clarify"] = not (approved or started)
    if operation == "start" or (manual_request and "clarify" in result):
        authorize()
        start_notice(gh, cfg, result)
    return result


def main():
    gh = GitHub(os.environ["GITHUB_REPOSITORY"])
    event = read_json(os.environ["GITHUB_EVENT_PATH"])
    try:
        result = receive(gh, event)
    except (Blocked, ValueError, KeyError) as exc:
        print(f"NexKit intake blocked: {exc}", file=sys.stderr)
        try:
            start_failure_notice(gh, event)
        except (Blocked, ValueError, KeyError):
            # Deleted/edited commands and revoked permissions cannot authorize
            # an issue write merely because intake failed.
            pass
        return 2
    print(canonical(result))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary and "issue" in result:
        issue = result["issue"]
        url = issue["html_url"] if isinstance(issue, dict) else issue
        with open(summary, "a") as out:
            out.write(f"NexKit work item: {url}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
