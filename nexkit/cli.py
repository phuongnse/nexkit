"""NexKit user commands. Human approvals are explicit workflow decisions."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .common import Blocked, canonical, consumer_path, digest, read_json
from .github import GitHub
from .pipelines import (
    dispatch,
    effective_config,
    entrypoint,
    issue_pipeline,
    pipeline_id,
    task_pipeline,
    work_entrypoint,
)
from .policy import config, require, spec_hash
from .project import HOSTS, doctor, install, survey, uninstall

SPEC_MARKER = "\n<!-- nexkit:spec -->\n"
MANUAL_SPEC_NOTICE = "<!-- nexkit:requirement-ready:"


def request_body(original, key, *, pipeline=None):
    """Use the same requirement layout for new and explicitly adopted issues."""
    require(
        isinstance(key, str)
        and len(key) <= 128
        and key.replace("-", "").replace("_", "").isalnum(),
        "Idempotency key must be a short identifier",
    )
    from .pipelines import IDENTIFIER

    require(
        isinstance(pipeline, str) and IDENTIFIER.fullmatch(pipeline), "Invalid request pipeline"
    )
    require(isinstance(original, str) and original.strip(), "Request body cannot be empty")
    scoped_key = f"{pipeline}:{key}"
    marker = f"<!-- nexkit:request:{scoped_key} -->"
    body = (
        marker
        + f"\n<!-- nexkit:pipeline:{pipeline} -->"
        + "\n<details>\n<summary>Original request</summary>\n\n"
        + "\n".join("> " + line for line in original.splitlines())
        + "\n\n</details>\n"
    )
    body += (
        SPEC_MARKER
        + "## Specification\n\nRequirement clarification pending. Do not implement yet.\n"
    )
    require(len(body.encode()) < 60000, "Requirement exceeds the issue size bound")
    return body


def create_request(gh, title, original, key=None, *, pipeline=None):
    """Called by the serialized intake workflow, never directly by user commands."""
    key = key or digest({"repository": gh.repository, "title": title, "request": original})
    body = request_body(original, key, pipeline=pipeline)
    marker = body.split("\n", 1)[0]
    issues = gh.api(f"{gh.root}/issues?state=all&per_page=100", pages=True)
    matches = [
        i
        for i in issues
        if not i.get("pull_request") and (i.get("body") or "").startswith(marker + "\n")
    ]
    require(len(matches) <= 1, "Multiple work items have this idempotency key")
    if matches:
        return {"created": False, "issue": matches[0]}
    issue = gh.api(f"{gh.root}/issues", "POST", {"title": title, "body": body})
    return {"created": True, "issue": issue}


def submit(gh, cfg, operation, payload):
    require(operation in ("request", "start", "spec", "release"), "Invalid intake operation")
    encoded = canonical(payload)
    require(len(encoded.encode()) <= 50000, "Intake payload exceeds 50 KB")
    dispatch(gh, cfg, "intake", {"operation": operation, "payload": encoded})
    key = payload.get("key", digest(payload))
    selection = f" --pipeline {pipeline_id(cfg)}" if pipeline_id(cfg) else ""
    result = {
        "queued": True,
        "operation": operation,
        "key": key,
        "lookup": f"nexkit{selection} intake-status --operation {operation} --key {key}",
        "pipeline": pipeline_id(cfg),
        "actions": f"https://github.com/{gh.repository}/actions/workflows/{Path(entrypoint(cfg, 'intake')).name}",
        "note": "The serialized GitHub intake run creates/reuses the issue; closing this terminal does not stop it.",
    }
    if operation in ("start", "spec"):
        result.pop("key")
        result.update(
            issue=payload["issue"],
            lookup=f"nexkit{selection} status {payload['issue']}",
            note=(
                "GitHub Actions publishes the specification and approval instructions as "
                "github-actions[bot]. Wait for that run before approving."
                if operation == "spec"
                else "The serialized GitHub intake run adopts this existing issue; requirement approval is still required."
            ),
        )
    return result


def intake_status(gh, operation, key, *, pipeline=None):
    from .release import RELEASE_MARKER, parse_candidate

    matches = []
    for issue in gh.api(f"{gh.root}/issues?state=all&per_page=100", pages=True):
        if issue.get("pull_request"):
            continue
        body = issue.get("body") or ""
        if operation == "request":
            scoped_key = f"{pipeline}:{key}" if pipeline else key
            matched = body.startswith(f"<!-- nexkit:request:{scoped_key} -->\n")
        elif body.startswith(RELEASE_MARKER):
            try:
                value = parse_candidate(issue)
            except Blocked:
                continue
            matched = (
                value.get("pipeline") == pipeline
                and digest({k: value[k] for k in ("commit", "version", "notes")}) == key
            )
        else:
            matched = False
        if matched:
            matches.append(issue)
    require(len(matches) <= 1, "Multiple issues match this submission; inspect the intake runs")
    return {
        "found": bool(matches),
        "issue": matches[0] if matches else None,
        "actions": f"https://github.com/{gh.repository}/actions",
    }


def specification_body(issue, body):
    require(SPEC_MARKER in (issue.get("body") or ""), "Not a NexKit request work item")
    require(isinstance(body, str) and body.strip(), "Specification cannot be empty")
    prefix = issue["body"].split(SPEC_MARKER, 1)[0]
    updated = prefix + SPEC_MARKER + body.rstrip() + "\n"
    require(len(updated.encode()) < 60000, "Requirement exceeds the issue size bound")
    return updated


def specification_payload(issue, body):
    """Bind queued manual publication to the issue the caller actually read."""
    require(issue.get("state") == "open", "Work item is closed")
    require(
        not issue.get("pull_request"), "Publish a specification on an issue, not a pull request"
    )
    return {
        "issue": issue["number"],
        "body": body,
        "expected": spec_hash(issue),
        "edited_at": issue.get("last_edited_at"),
        "target": spec_hash({**issue, "body": specification_body(issue, body)}),
    }


def set_spec(gh, number, body, *, expected=None, before_write=None):
    issue = gh.issue(number)
    if expected is not None:
        require(
            spec_hash(issue) == spec_hash(expected)
            and issue.get("last_edited_at") == expected.get("last_edited_at"),
            "Requirement changed before publication",
        )
    updated = specification_body(issue, body)
    if before_write is not None:
        before_write(issue)
    if issue["body"] != updated:
        issue = gh.api(f"{gh.root}/issues/{number}", "PATCH", {"body": updated})
    return {
        "issue": issue["html_url"],
        "spec": spec_hash(issue),
        "human_approval_comment": "/nexkit approve " + spec_hash(issue),
    }


def requirement_approval_text(spec):
    return (
        "**Ready for requirement approval.** A collaborator with write, maintain or admin "
        "access can approve this version by commenting:\n\n"
        f"`/nexkit approve {spec}`\n\n"
        "Read the specification above before approving. If it needs changes, discuss them "
        "on this issue and update the specification before approving its new version."
    )


def parser():
    p = argparse.ArgumentParser(
        prog="nexkit", description="NexKit — coding workflows with explicit approvals."
    )
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--root", default=".", help="Consumer repository directory")
    p.add_argument("--pipeline", help="Explicit consumer pipeline for pipeline operations")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("survey", help="Read repository context for the setup agent")
    c = sub.add_parser("use", help="Select an exact published NexKit release, higher or lower")
    c.add_argument("--version", required=True, help="Release version, such as 1.1.0 or 1.0.0")
    c.add_argument("--dry-run", action="store_true", help="Show changes without applying them")
    for name in ("setup", "install"):
        c = sub.add_parser(name, help="Preview/apply accepted setup or versioned kit update")
        c.add_argument("--config", required=True, help="User-approved project JSON")
        c.add_argument("--host", choices=HOSTS, default="codex", help="Project skills target")
        c.add_argument("--apply", action="store_true")
        c.add_argument("--online", action="store_true")
        c.add_argument("--bundle", help="Directory containing the accepted workflow/control files")
    c = sub.add_parser("doctor", help="Verify local configuration and optional live setup")
    c.add_argument("--online", action="store_true")
    c.add_argument("--checks", action="store_true")
    sub.add_parser("uninstall", help="Remove unchanged kit-managed files, preserve consumer data")
    c = sub.add_parser(
        "request", help="Create the GitHub work item before specification/implementation"
    )
    c.add_argument("--title", required=True)
    c.add_argument("--body-file", required=True)
    c.add_argument("--key")
    c = sub.add_parser("start", help="Receive an existing GitHub issue without creating another")
    c.add_argument("issue", type=int)
    c = sub.add_parser("intake-status", help="Find the issue created by a queued submission")
    c.add_argument("--operation", choices=("request", "release"), default="request")
    c.add_argument("--key", required=True)
    c = sub.add_parser(
        "spec", help="Queue publication of the issue specification and Actions approval notice"
    )
    c.add_argument("issue", type=int)
    c.add_argument("--body-file", required=True)
    for name in ("approval", "status", "cancel", "resume"):
        c = sub.add_parser(name)
        c.add_argument("issue", type=int)
    c = sub.add_parser("complete", help="Preview or retry issue closure for completed work")
    c.add_argument("issue", type=int)
    c.add_argument("--apply", action="store_true", help="Close only verified completed issues")
    sub.add_parser("howto", help="Show approval steps and the configured workflow")
    sub.add_parser("knowledge", help="List this consumer's accepted knowledge sources")
    c = sub.add_parser("release", help="Prepare a specific unreleased candidate for approval")
    c.add_argument("--commit", required=True)
    c.add_argument("--version", required=True)
    c.add_argument("--notes-file", required=True)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    root = Path(args.root).resolve()
    try:
        if args.command == "howto":
            print(
                "Install NexKit for Codex → describe your project with nexkit-init → "
                "review setup decisions → "
                "nexkit-request creates an issue and clarifies its spec.\n"
                "Existing issue: comment /nexkit start PIPELINE, or run nexkit --pipeline PIPELINE start ISSUE.\n"
                "Requirement approval: post the exact /nexkit approve HASH comment on that issue.\n"
                "Actions implements, verifies, independently reviews, repairs and merges within limits.\n"
                "Additional stage approvals may be configured for selected stages during setup.\n"
                "Track the issue, PR and Actions; nexkit status/cancel/resume manage interrupted work.\n"
                "Release decision: post /nexkit release HASH on a prepared release candidate issue.\n"
                "A merge never starts a release. Credentials and policy changes belong to setup."
            )
            return 0
        if args.command == "survey":
            result = survey(root)
        elif args.command == "use":
            from .versions import use

            result = use(root, args.version, apply=not args.dry_run)
        elif args.command in ("setup", "install"):
            cfg = config(read_json(args.config))
            result = install(root, cfg, [args.host], apply=args.apply, bundle=args.bundle)
            if args.apply and args.command == "setup":
                result["verification"] = doctor(root, cfg, online=args.online, checks=True)
        elif args.command == "uninstall":
            result = uninstall(root)
        else:
            project_cfg = config(read_json(consumer_path(root, ".nexkit/project.json")))
            if args.command == "doctor":
                result = doctor(root, project_cfg, online=args.online, checks=args.checks)
            else:
                gh = GitHub(project_cfg["repository"])
                selected = args.pipeline
                if hasattr(args, "issue") and args.command != "start":
                    issue = gh.issue(args.issue)
                    if args.command == "complete":
                        completed, _ = gh.get_state(args.issue)
                        recorded = completed.get("pipeline")
                        require(recorded, "Completed work is missing its recorded pipeline")
                    elif (issue.get("body") or "").startswith("<!-- nexkit:release -->"):
                        from .release import parse_candidate

                        recorded = parse_candidate(issue).get("pipeline")
                    else:
                        recorded = issue_pipeline(issue)
                    require(
                        selected is None or selected == recorded,
                        "Work item belongs to another pipeline",
                    )
                    selected = recorded
                cfg = effective_config(project_cfg, selected)
                if args.command == "knowledge":
                    result = {"decisions": cfg["decisions"], "sources": cfg["knowledge"]}
                elif args.command == "request":
                    original = Path(args.body_file).read_text(encoding="utf-8")
                    key = args.key or digest(
                        {"repository": gh.repository, "title": args.title, "request": original}
                    )
                    result = submit(
                        gh, cfg, "request", {"title": args.title, "request": original, "key": key}
                    )
                elif args.command == "spec":
                    result = submit(
                        gh,
                        cfg,
                        "spec",
                        specification_payload(
                            issue, Path(args.body_file).read_text(encoding="utf-8")
                        ),
                    )
                elif args.command == "start":
                    require(args.issue > 0, "Use a positive GitHub issue number")
                    result = submit(gh, cfg, "start", {"issue": args.issue})
                elif args.command == "intake-status":
                    result = intake_status(gh, args.operation, args.key, pipeline=pipeline_id(cfg))
                elif args.command == "approval":
                    issue = gh.issue(args.issue)
                    verb = (
                        "release"
                        if (issue.get("body") or "").startswith("<!-- nexkit:release -->")
                        else "approve"
                    )
                    result = {
                        "issue": issue["html_url"],
                        "human_comment": f"/nexkit {verb} {spec_hash(issue)}",
                        "note": "An authorized collaborator posts this on GitHub; agents cannot approve requirements.",
                    }
                elif args.command == "complete":
                    from .completion import reconcile

                    state = reconcile(gh, args.issue, apply=args.apply)
                    result = {
                        "issue": issue["html_url"],
                        "apply": args.apply,
                        "status": state["status"],
                        "completion": state["completion"],
                    }
                elif args.command == "status":
                    state, _ = gh.get_state(args.issue)
                    result = {
                        "issue": gh.issue(args.issue)["html_url"],
                        "work" if task_pipeline(cfg) else "delivery": state
                        or {"status": "awaiting requirement approval"},
                    }
                    if state.get("status") == "waiting_for_approval":
                        from datetime import datetime

                        from .policy import now

                        record = state["stage_approvals"][state["approval_wait"]["gate"]]
                        result["approval_wait_expired"] = (
                            datetime.fromisoformat(now())
                            - datetime.fromisoformat(record["opened_at"])
                        ).total_seconds() >= record["definition"]["wait_minutes"] * 60
                elif args.command in ("cancel", "resume"):
                    result = gh.comment(args.issue, f"/nexkit {args.command}")
                    if args.command == "resume":
                        issue = gh.issue(args.issue)
                        state, _ = gh.get_state(args.issue)
                        from .approvals import recovery_gate

                        pending_gate = recovery_gate(state)
                        if pending_gate:
                            gate = state["stage_approvals"][pending_gate]
                            gh.dispatch(
                                gate["definition"]["continuation"].removeprefix(
                                    ".github/workflows/"
                                ),
                                cfg["default_branch"],
                                {"issue": args.issue},
                            )
                            print(
                                json.dumps(
                                    {
                                        "queued": True,
                                        "gate": gate["gate"],
                                        "note": "An existing valid approval is still required.",
                                    },
                                    indent=2,
                                )
                            )
                            return 0
                        workflow = (
                            "release"
                            if (issue.get("body") or "").startswith("<!-- nexkit:release -->")
                            else work_entrypoint(cfg)
                        )
                        if (
                            workflow in {"delivery", "tasks"}
                            and state.get("clarification")
                            and not state.get("approval")
                        ):
                            workflow = "clarify"
                        dispatch(gh, cfg, workflow, {"issue": args.issue})
                elif args.command == "release":
                    from .policy import SHA, VERSION

                    require(
                        SHA.fullmatch(args.commit) and VERSION.fullmatch(args.version),
                        "Use an exact release commit/version",
                    )
                    require(cfg["release"]["enabled"], "Release is not configured")
                    result = submit(
                        gh,
                        cfg,
                        "release",
                        {
                            "commit": args.commit,
                            "version": args.version,
                            "notes": Path(args.notes_file).read_text(encoding="utf-8"),
                        },
                    )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command == "complete" and result["completion"].get("error"):
            return 2
        if args.command == "doctor" and not result["ready"]:
            return 2
        if args.command == "setup" and args.apply and not result["verification"]["ready"]:
            return 2
        return 0
    except (Blocked, OSError, ValueError, KeyError) as exc:
        print(f"NexKit blocked: {exc}", file=sys.stderr)
        return 2
