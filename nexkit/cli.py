"""Command line entry point.

`init`, `doctor` and `status` are for people working in a repository. The other commands are the
steps of the reusable GitHub Actions pipeline and read their inputs from the environment.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

from . import CLAUDE_CODE, __version__, python_error, python_supported, scaffold
from . import config as configuration


def _output(**values):
    """Write step outputs for GitHub Actions (or print them when run locally)."""
    path = os.environ.get("GITHUB_OUTPUT")
    lines = []
    for key, value in values.items():
        text = value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))
        delimiter = f"EOF_{uuid.uuid4().hex}"
        lines.append(f"{key}<<{delimiter}\n{text}\n{delimiter}\n")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.writelines(lines)
    else:
        sys.stdout.writelines(lines)


def _decision():
    return json.loads(os.environ["NEXKIT_DECISION"])


def _config():
    return configuration.validate(json.loads(os.environ["NEXKIT_CONFIG"]))


def _gh():
    from .github import GitHub

    return GitHub()


def cmd_init(args):
    written = scaffold.init(
        args.repo,
        checks=args.check,
        setup=args.setup,
        model=args.model,
        kit_repo=args.kit_repo,
        kit_ref=args.kit_ref,
        force=args.force,
    )
    for path in written:
        print(f"wrote {path}")
    print("Next: review the files, commit them to the default branch, then run 'nexkit doctor'.")
    return 0


def cmd_doctor(args):
    findings = scaffold.doctor(args.repo)
    for ok, message in findings:
        print(f"{'✓' if ok else '✗'} {message}")
    return 0 if all(ok for ok, _ in findings) else 1


def cmd_status(args):
    from .github import GitHub, GitHubError
    from .status import collect, render, to_json

    repository = args.repository or scaffold.repository_of(args.repo)
    if not repository:
        print(
            "nexkit: cannot tell the GitHub repository; pass --repository OWNER/REPO",
            file=sys.stderr,
        )
        return 2
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or scaffold.gh_token()
    if not token:
        print("nexkit: sign in with 'gh auth login' or set GITHUB_TOKEN", file=sys.stderr)
        return 2
    try:
        cfg = configuration.load(args.repo)
    except configuration.ConfigError:
        cfg = {}
    try:
        data = collect(GitHub(repository=repository, token=token), cfg, merges=args.merges)
    except GitHubError as exc:
        print(f"nexkit: {exc}", file=sys.stderr)
        return 1
    print(to_json(data) if args.json else render(data))
    return 0


def cmd_route(args):
    from . import progress
    from .github import GitHubError
    from .maintain import TASKS, enabled
    from .route import route, with_profile

    gh = _gh()
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    decision = route(gh, os.environ["GITHUB_EVENT_NAME"], event, event.get("inputs") or {})
    cfg = None
    if decision["action"] != "none":
        try:
            cfg = configuration.load(args.repo)
        except configuration.ConfigError as exc:
            decision = {
                "action": "none",
                "reason": f"NexKit configuration error: {exc}",
                "reply_to": decision.get("target"),
            }
        else:
            decision, cfg = with_profile(gh, decision, cfg)
    if decision["action"] == "maintain" and not enabled(decision["task"], cfg):
        keys = " and ".join(TASKS[decision["task"]])
        decision = {"action": "none", "reason": f"Nothing to do: {keys} off"}
    print(json.dumps(decision, indent=2))
    if decision["action"] == "maintain":
        _output(action="maintain", decision=decision, config=cfg)
        return 0
    if decision["action"] == "none":
        if decision.get("reply_to"):
            try:
                gh.comment(decision["reply_to"], decision["reason"])
            except GitHubError as exc:
                print(f"Could not reply: {exc}", file=sys.stderr)
        _output(action="none", decision=decision, config={})
        return 0
    progress.start(gh, decision)
    agent_minutes = configuration.stage(cfg, decision["action"])["timeout_minutes"]
    if decision["action"] == "plan" and cfg["profiles"]:
        # Triage chooses the profile inside the job, so allow for the longest plan.
        longest = max(
            configuration.stage(configuration.with_profile(cfg, name), "plan")["timeout_minutes"]
            for name in cfg["profiles"]
        )
        agent_minutes = configuration.stage(cfg, "triage")["timeout_minutes"] + longest
    review_stage = configuration.stage(cfg, "review")
    checks_minutes = sum(c["timeout_minutes"] for c in cfg["checks"])
    _output(
        action=decision["action"],
        decision=decision,
        config=cfg,
        head=decision.get("head") or "",
        base=decision["base"],
        branch=decision["branch"],
        agent_ref=decision["branch"] if decision["action"] == "fix" else decision["base"],
        agent_timeout=str(agent_minutes + 30),
        review_timeout=str(review_stage["timeout_minutes"] + 30),
        checks_timeout=str(checks_minutes + 40),
        claude_version=CLAUDE_CODE,
    )
    return 0


def cmd_context(args):
    from .context import gather

    decision = _decision()
    if args.pr:
        decision["pr"] = int(args.pr)
    fix_result = None
    if args.fix_result and Path(args.fix_result).is_file():
        fix_result = json.loads(Path(args.fix_result).read_text())
    context = gather(_gh(), decision, stage=args.stage, fix_result=fix_result)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "context.json").write_text(json.dumps(context, indent=2))
    return 0


def cmd_agent(args):
    from .agent import conflicts_with_base, run_stage, run_triage, up_to_date
    from .checks import find_base, setup
    from .redact import Redactor
    from .runlog import summary, write_summary

    decision = _decision()
    cfg = _config()
    stage = args.stage or decision["action"]
    base = decision["base"]
    out = Path(args.out)
    context = json.loads((out / "context.json").read_text())
    base_sha = None
    if stage == "fix" and decision.get("conflicts") and not conflicts_with_base(args.repo, base):
        # Another round or a person resolved it since this round was started.
        result = up_to_date(base)
        (out / "result.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2))
        return 0
    if stage in ("implement", "fix"):
        base_sha = find_base(args.repo, decision["base"])
        redact = Redactor()
        failed = setup(cfg, args.repo, redact, base_sha)
        if failed:
            result = {
                "stage": stage,
                "status": "error",
                "error": f"Setup command `{failed['run']}` failed with exit code "
                f"{failed['exit_code']}:\n```\n{failed['output'][-2000:]}\n```",
                "cost": None,
            }
            (out / "result.json").write_text(json.dumps(result, indent=2))
            write_summary(summary(result, []), redact)
            return 0
    triage = None
    if stage == "plan" and cfg["profiles"]:
        triage = run_triage(context, cfg, decision.get("previous_profile"), out)
        if triage["status"] == "paused":
            # The plan would stop at the same limit; it runs again after the reset.
            keys = ("status", "error", "limit", "resume_at", "reset_known", "cost")
            result = {"stage": stage, **{key: triage[key] for key in keys}}
            (out / "result.json").write_text(json.dumps(result, indent=2))
            print(json.dumps(result, indent=2))
            return 0
        cfg = configuration.with_profile(cfg, triage["profile"])
    checks = json.loads(Path(args.checks).read_text()) if args.checks else None
    result = run_stage(
        stage,
        context,
        cfg,
        args.repo,
        out,
        checks=checks,
        base=decision["base"],
        base_sha=base_sha,
        triage=triage,
    )
    print(json.dumps({k: v for k, v in result.items() if k != "output"}, indent=2))
    return 0


def cmd_checks(args):
    from .checks import find_base, run_checks

    base_sha = find_base(args.repo, _decision()["base"])
    results = run_checks(_config(), args.repo, args.out, base_sha)
    passed = all(r["passed"] for r in results)
    print(f"{len(results)} checks, {'all passed' if passed else 'some failed'}")
    return 0


def cmd_publish(args):
    from .github import GitHub, GitHubError
    from .gitutil import GitError
    from .publish import PublishError, publish

    decision = _decision()
    out = Path(args.artifacts)
    result = json.loads((out / "result.json").read_text())
    context = json.loads((out / "context.json").read_text())
    bot = GitHub()
    push_token = os.environ.get("NEXKIT_PUSH_TOKEN")
    author = GitHub(token=push_token) if push_token else bot
    try:
        outcome = publish(
            bot,
            decision,
            result,
            context,
            _config(),
            args.repo,
            out,
            author=author,
            push_token=bool(push_token),
        )
    except (PublishError, GitError, GitHubError) as exc:
        outcome = {"published": False, "error": str(exc)}
    print(json.dumps(outcome, indent=2))
    _output(result=outcome, head=outcome.get("head") or "", pr=str(outcome.get("pr") or ""))
    return 0


def cmd_report(args):
    from .report import report

    gh = _gh()
    outcome = report(
        gh,
        _decision(),
        _config(),
        json.loads(os.environ.get("NEXKIT_NEEDS") or "{}"),
        args.artifacts,
        workflow_ref=os.environ.get("GITHUB_WORKFLOW_REF", ""),
        default_branch=os.environ.get("NEXKIT_DEFAULT_BRANCH") or gh.default_branch(),
    )
    print(json.dumps(outcome, indent=2))
    return 0


def cmd_maintain(args):
    from .maintain import maintain
    from .report import workflow_file

    gh = _gh()
    lines = maintain(
        gh,
        _decision(),
        _config(),
        workflow=workflow_file(os.environ.get("GITHUB_WORKFLOW_REF", "")),
        ref=os.environ.get("NEXKIT_DEFAULT_BRANCH") or gh.default_branch(),
    )
    for line in lines:
        print(line)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="nexkit", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version=f"nexkit {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="Install NexKit configuration and workflow in a repository")
    p.add_argument("--repo", default=".", help="Repository root (default: current directory)")
    p.add_argument("--check", action="append", default=[], metavar="NAME=COMMAND")
    p.add_argument("--setup", action="append", default=[], metavar="COMMAND")
    p.add_argument("--model", default=configuration.DEFAULTS["model"])
    p.add_argument("--kit-repo", default=scaffold.KIT_REPOSITORY)
    p.add_argument("--kit-ref", default=None, help=f"Default: v{__version__}")
    p.add_argument("--force", action="store_true", help="Overwrite existing files")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("doctor", help="Check a repository's NexKit setup")
    p.add_argument("--repo", default=".")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("status", help="List open NexKit work and what each item waits for")
    p.add_argument("--repo", default=".", help="Repository root (default: current directory)")
    p.add_argument("--repository", help="OWNER/REPO (default: the 'origin' remote)")
    p.add_argument("--json", action="store_true", help="Print JSON")
    p.add_argument("--merges", type=int, default=5, help="Recent merges to show (default 5)")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("route", help="(pipeline) Decide the action for the current event")
    p.add_argument("--repo", required=True, help="Checkout of the default branch")
    p.set_defaults(func=cmd_route)

    p = sub.add_parser("context", help="(pipeline) Collect GitHub context for an agent")
    p.add_argument("--out", required=True)
    p.add_argument("--pr", default="", help="Pull request number when it was just created")
    p.add_argument("--stage", choices=["review"], help="Default: the decision's action")
    p.add_argument("--fix-result", help="This run's fix result.json, for the review stage")
    p.set_defaults(func=cmd_context)

    p = sub.add_parser("agent", help="(pipeline) Run a Claude Code stage")
    p.add_argument("--repo", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--stage", choices=["plan", "implement", "fix", "review"])
    p.add_argument("--checks", help="checks.json for the review stage")
    p.set_defaults(func=cmd_agent)

    p = sub.add_parser("checks", help="(pipeline) Run setup and checks on a candidate")
    p.add_argument("--repo", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_checks)

    p = sub.add_parser("publish", help="(pipeline) Commit, push and open pull requests")
    p.add_argument("--repo", required=True)
    p.add_argument("--artifacts", required=True)
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser("maintain", help="(pipeline) Work that follows events, without an agent")
    p.set_defaults(func=cmd_maintain)

    p = sub.add_parser("report", help="(pipeline) Report results and schedule the next round")
    p.add_argument("--artifacts", required=True)
    p.set_defaults(func=cmd_report)
    return parser


def main(argv=None):
    if not python_supported(sys.version_info):
        print(python_error(sys.version_info), file=sys.stderr)
        return 2
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (configuration.ConfigError, FileExistsError) as exc:
        print(f"nexkit: {exc}", file=sys.stderr)
        return 2
