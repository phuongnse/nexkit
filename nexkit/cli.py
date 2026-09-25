"""NexKit user commands. Human approvals are made explicitly on GitHub."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .common import Blocked, canonical, consumer_path, digest, read_json, write_json
from .github import GitHub
from .policy import config, require, spec_hash
from .project import HOSTS, doctor, install, survey, uninstall

SPEC_MARKER = "\n<!-- nexkit:spec -->\n"


def create_request(gh, title, original, key=None):
    """Called by the serialized intake workflow, never directly by user commands."""
    key = key or digest({"repository": gh.repository, "title": title, "request": original})
    require(
        isinstance(key, str)
        and len(key) <= 128
        and key.replace("-", "").replace("_", "").isalnum(),
        "Idempotency key must be a short identifier",
    )
    marker = f"<!-- nexkit:request:{key} -->"
    issues = gh.api(f"{gh.root}/issues?state=all&per_page=100", pages=True)
    matches = [
        i
        for i in issues
        if not i.get("pull_request") and (i.get("body") or "").startswith(marker + "\n")
    ]
    require(len(matches) <= 1, "Multiple work items have this idempotency key")
    if matches:
        return {"created": False, "issue": matches[0]}
    body = (
        marker
        + "\n## Original request\n\n"
        + "\n".join("> " + line for line in original.splitlines())
    )
    body += (
        SPEC_MARKER
        + "## Specification\n\nRequirement clarification pending. Do not implement yet.\n"
    )
    issue = gh.api(f"{gh.root}/issues", "POST", {"title": title, "body": body})
    return {"created": True, "issue": issue}


def submit(gh, cfg, operation, payload):
    require(operation in ("request", "release"), "Invalid intake operation")
    encoded = canonical(payload)
    require(len(encoded.encode()) <= 50000, "Intake payload exceeds 50 KB")
    gh.dispatch(
        "nexkit-intake.yml", cfg["default_branch"], {"operation": operation, "payload": encoded}
    )
    key = payload.get("key", digest(payload))
    return {
        "queued": True,
        "operation": operation,
        "key": key,
        "lookup": f"nexkit intake-status --operation {operation} --key {key}",
        "actions": f"https://github.com/{gh.repository}/actions/workflows/nexkit-intake.yml",
        "note": "The serialized GitHub intake run creates/reuses the issue; closing this terminal does not stop it.",
    }


def intake_status(gh, operation, key):
    from .release import RELEASE_MARKER, parse_candidate

    matches = []
    for issue in gh.api(f"{gh.root}/issues?state=all&per_page=100", pages=True):
        if issue.get("pull_request"):
            continue
        body = issue.get("body") or ""
        if operation == "request":
            matched = body.startswith(f"<!-- nexkit:request:{key} -->\n")
        elif body.startswith(RELEASE_MARKER):
            value = parse_candidate(issue)
            matched = digest({k: value[k] for k in ("commit", "version", "notes")}) == key
        else:
            matched = False
        if matched:
            matches.append(issue)
    require(len(matches) <= 1, "Multiple issues match this submission; inspect the intake runs")
    return {
        "found": bool(matches),
        "issue": matches[0] if matches else None,
        "actions": f"https://github.com/{gh.repository}/actions/workflows/nexkit-intake.yml",
    }


def set_spec(gh, number, body):
    issue = gh.issue(number)
    require(SPEC_MARKER in (issue.get("body") or ""), "Not a NexKit request work item")
    require(body.strip(), "Specification cannot be empty")
    prefix = issue["body"].split(SPEC_MARKER, 1)[0]
    issue = gh.api(
        f"{gh.root}/issues/{number}", "PATCH", {"body": prefix + SPEC_MARKER + body.rstrip() + "\n"}
    )
    return {
        "issue": issue["html_url"],
        "spec": spec_hash(issue),
        "human_approval_comment": "/nexkit approve " + spec_hash(issue),
    }


def parser():
    p = argparse.ArgumentParser(prog="nexkit", description="NexKit — Agents. Skills. One workflow.")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--root", default=".", help="Consumer repository directory")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("survey", help="Read repository context for the setup agent")
    for name in ("setup", "install"):
        c = sub.add_parser(name, help="Preview/apply accepted setup or versioned kit update")
        c.add_argument("--config", required=True, help="User-approved project JSON")
        c.add_argument("--host", action="append", choices=HOSTS, required=True)
        c.add_argument("--apply", action="store_true")
        c.add_argument("--online", action="store_true")
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
    c = sub.add_parser("intake-status", help="Find the issue created by a queued submission")
    c.add_argument("--operation", choices=("request", "release"), default="request")
    c.add_argument("--key", required=True)
    c = sub.add_parser(
        "spec", help="Update the issue's authoritative specification before approval"
    )
    c.add_argument("issue", type=int)
    c.add_argument("--body-file", required=True)
    for name in ("approval", "status", "cancel", "resume"):
        c = sub.add_parser(name)
        c.add_argument("issue", type=int)
    sub.add_parser("howto", help="Show the two human decisions and normal workflow")
    sub.add_parser("knowledge", help="List this consumer's accepted knowledge sources")
    c = sub.add_parser("release", help="Prepare a specific unreleased candidate for human decision")
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
                "Install → describe your project with nexkit-init → review setup decisions → "
                "nexkit-request creates an issue and clarifies its spec.\n"
                "Human decision 1: post the exact /nexkit approve HASH comment on that issue.\n"
                "Actions implements, verifies, independently reviews, repairs and merges within limits.\n"
                "Track the issue, PR and Actions; nexkit status/cancel/resume manage interrupted work.\n"
                "Human decision 2: post /nexkit release HASH on a prepared release candidate issue.\n"
                "A merge never starts a release. Credentials and policy changes belong to setup."
            )
            return 0
        if args.command == "survey":
            result = survey(root)
        elif args.command in ("setup", "install"):
            cfg = config(read_json(args.config))
            result = install(root, cfg, args.host, apply=args.apply)
            if args.apply and args.command == "setup":
                write_json(consumer_path(root, ".nexkit/project.json"), cfg)
                result["verification"] = doctor(root, cfg, online=args.online, checks=True)
        elif args.command == "uninstall":
            result = uninstall(root)
        else:
            cfg = config(read_json(consumer_path(root, ".nexkit/project.json")))
            if args.command == "doctor":
                result = doctor(root, cfg, online=args.online, checks=args.checks)
            elif args.command == "knowledge":
                result = {"decisions": cfg["decisions"], "sources": cfg["knowledge"]}
            else:
                gh = GitHub(cfg["repository"])
                if args.command == "request":
                    original = Path(args.body_file).read_text()
                    key = args.key or digest(
                        {"repository": gh.repository, "title": args.title, "request": original}
                    )
                    result = submit(
                        gh, cfg, "request", {"title": args.title, "request": original, "key": key}
                    )
                elif args.command == "spec":
                    result = set_spec(gh, args.issue, Path(args.body_file).read_text())
                elif args.command == "intake-status":
                    result = intake_status(gh, args.operation, args.key)
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
                        "note": "The authorized human posts this on GitHub; agents cannot approve requirements.",
                    }
                elif args.command == "status":
                    state, _ = gh.get_state(args.issue)
                    result = {
                        "issue": gh.issue(args.issue)["html_url"],
                        "delivery": state or {"status": "awaiting requirement approval"},
                    }
                elif args.command in ("cancel", "resume"):
                    result = gh.comment(args.issue, f"/nexkit {args.command}")
                    if args.command == "resume":
                        issue = gh.issue(args.issue)
                        state, _ = gh.get_state(args.issue)
                        workflow = (
                            "release"
                            if (issue.get("body") or "").startswith("<!-- nexkit:release -->")
                            else "delivery"
                        )
                        if (
                            workflow == "delivery"
                            and state.get("clarification")
                            and not state.get("approval")
                        ):
                            workflow = "clarify"
                        gh.dispatch(
                            f"nexkit-{workflow}.yml", cfg["default_branch"], {"issue": args.issue}
                        )
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
                            "notes": Path(args.notes_file).read_text(),
                        },
                    )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command == "doctor" and not result["ready"]:
            return 2
        if args.command == "setup" and args.apply and not result["verification"]["ready"]:
            return 2
        return 0
    except (Blocked, OSError, ValueError, KeyError) as exc:
        print(f"NexKit blocked: {exc}", file=sys.stderr)
        return 2
