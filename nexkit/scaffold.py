"""`nexkit init` and `nexkit doctor`: install and check a repository's NexKit setup."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from importlib import resources
from pathlib import Path

from . import __version__
from . import config as configuration

KIT_REPOSITORY = "phuongnse/nexkit"
WORKFLOW_PATH = ".github/workflows/nexkit.yml"
SECRETS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")


def workflow_text(kit_repo, kit_ref, default_branch="main"):
    template = resources.files("nexkit").joinpath("templates/workflow.yml").read_text()
    for name, value in (
        ("__KIT_REPO__", kit_repo),
        ("__KIT_REF__", kit_ref),
        ("__DEFAULT_BRANCH__", default_branch),
    ):
        template = template.replace(name, value)
    return template


def default_branch_of(root):
    """The default branch of `origin`, else the current branch, else `main`."""
    for args in (
        ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
        ["symbolic-ref", "--quiet", "--short", "HEAD"],
    ):
        proc = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
        name = proc.stdout.strip().removeprefix("origin/")
        if proc.returncode == 0 and re.fullmatch(r"[\w./-]+", name):
            return name
    return "main"


def parse_check(value):
    name, sep, command = value.partition("=")
    if not sep or not name.strip() or not command.strip():
        raise configuration.ConfigError(f"--check must look like NAME=COMMAND, got '{value}'")
    return {"name": name.strip(), "run": command.strip()}


def init(root, *, checks, setup, model, kit_repo, kit_ref, force):
    root = Path(root)
    config_path = root / configuration.CONFIG_PATH
    workflow_path = root / WORKFLOW_PATH
    if not force:
        existing = [str(p.relative_to(root)) for p in (config_path, workflow_path) if p.exists()]
        if existing:
            raise FileExistsError(f"Already exists: {', '.join(existing)} (use --force)")
    raw = {
        "version": 1,
        "model": model,
        "setup": setup,
        "checks": [parse_check(c) for c in checks],
        "max_auto_fixes": configuration.DEFAULTS["max_auto_fixes"],
        "auto_merge": False,
    }
    configuration.validate(raw)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(raw, indent=2) + "\n")
    workflow_path.parent.mkdir(parents=True, exist_ok=True)
    workflow_path.write_text(
        workflow_text(kit_repo, kit_ref or f"v{__version__}", default_branch_of(root))
    )
    return [configuration.CONFIG_PATH, WORKFLOW_PATH]


def _gh(*args):
    if not shutil.which("gh"):
        return None
    proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else None


def repository_of(root):
    proc = subprocess.run(
        ["git", "remote", "get-url", "origin"], cwd=root, capture_output=True, text=True
    )
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", proc.stdout.strip())
    return match.group(1) if match else None


def _uncommented(text):
    return re.sub(r"(^|\s)#.*", "", text)


def _after_merge_workflow(root, name):
    """Whether NexKit can start a workflow of after_merge_workflows with a dispatch."""
    path = root / ".github/workflows" / name
    if not path.is_file():
        return (False, f"after_merge_workflows: {path.relative_to(root)} does not exist")
    text = _uncommented(path.read_text(errors="replace"))
    if not re.search(r"\bworkflow_dispatch\b", text):
        return (
            False,
            f"after_merge_workflows: {name} has no workflow_dispatch trigger, "
            "so NexKit cannot start it after a merge",
        )
    return (True, f"after_merge_workflows: {name} can be started after a merge")


def _concurrency(workflow):
    """A finding: the concurrency group must be on the job, so plain comments never join it."""
    if re.search(r"^    concurrency:", workflow, re.M) and not re.search(
        r"^concurrency:", workflow, re.M
    ):
        return (True, "Commands on one issue or pull request run one at a time")
    return (
        False,
        f"Set 'concurrency' on the 'nexkit' job in {WORKFLOW_PATH}, as 'nexkit init' writes "
        "it (see docs/troubleshooting.md)",
    )


def trigger(workflow, event):
    """The text under `event:` in the workflow's `on:` block, or None without that trigger."""
    block = re.search(r"^on:[ \t]*\n((?:[ \t]+.*\n|[ \t]*\n)*)", _uncommented(workflow), re.M)
    if not block:
        return None
    found = re.search(rf"^  {event}:(.*\n(?:    .*\n|[ \t]*\n)*)", block.group(1), re.M)
    return found.group(1) if found else None


def _triggers(cfg, workflow):
    """Findings for the triggers that configuration keys need."""
    findings = []
    if cfg.get("close_parent_issues"):
        issues = trigger(workflow, "issues")
        if issues is None or ("types" in issues and "closed" not in issues):
            findings.append(
                (
                    False,
                    f"close_parent_issues is on, but {WORKFLOW_PATH} does not listen to "
                    "'issues: closed'; add it as 'nexkit init' writes it",
                )
            )
        else:
            findings.append((True, "Closed issues start NexKit, for close_parent_issues"))
    if cfg.get("auto_resolve_conflicts"):
        if trigger(workflow, "push") is None:
            findings.append(
                (
                    False,
                    f"auto_resolve_conflicts is on, but {WORKFLOW_PATH} has no 'push' trigger "
                    "for the default branch, so merges by people are noticed only by the "
                    "hourly schedule; add it as 'nexkit init' writes it",
                )
            )
        else:
            findings.append((True, "Pushes to the default branch start the conflict check"))
    if cfg.get("resume_after_usage_limit"):
        if trigger(workflow, "schedule") is None:
            findings.append(
                (
                    False,
                    f"{WORKFLOW_PATH} has no 'schedule' trigger, so runs paused at the Claude "
                    "usage limit cannot resume; add it as 'nexkit init' writes it, or set "
                    "resume_after_usage_limit to false",
                )
            )
        else:
            findings.append((True, "A schedule resumes runs paused at the Claude usage limit"))
    return findings


def doctor(root):
    """Return a list of (ok, message) findings."""
    root = Path(root)
    findings = []
    cfg = None
    try:
        cfg = configuration.load(root)
        findings.append((True, f"{configuration.CONFIG_PATH} is valid"))
        if not cfg["checks"]:
            findings.append((False, "No checks configured; NexKit cannot verify changes"))
        findings += [_after_merge_workflow(root, name) for name in cfg["after_merge_workflows"]]
    except configuration.ConfigError as exc:
        findings.append((False, str(exc)))

    workflow = root / WORKFLOW_PATH
    if not workflow.is_file():
        findings.append((False, f"Missing {WORKFLOW_PATH}; run 'nexkit init'"))
    else:
        text = workflow.read_text()
        match = re.search(r"uses:\s*(\S+)/\.github/workflows/pipeline\.yml@(\S+)", text)
        if match:
            findings.append((True, f"Workflow uses {match.group(1)} pipeline at {match.group(2)}"))
        else:
            findings.append((False, f"{WORKFLOW_PATH} does not call the NexKit pipeline"))
        findings.append(_concurrency(text))
        if cfg:
            findings += _triggers(cfg, text)

    repo = repository_of(root)
    if not repo:
        findings.append((False, "The 'origin' remote is not a GitHub repository"))
        return findings
    if _gh("auth", "status") is None:
        findings.append((False, "GitHub CLI is missing or not signed in; skipped GitHub checks"))
        return findings

    names = set(
        (_gh("secret", "list", "--repo", repo, "--json", "name", "-q", ".[].name") or "").split()
    )
    if names & set(SECRETS):
        findings.append(
            (True, "Claude credential secret is set: " + ", ".join(sorted(names & set(SECRETS))))
        )
    else:
        findings.append(
            (
                False,
                "Set a Claude credential: run 'claude setup-token' and "
                f"'gh secret set CLAUDE_CODE_OAUTH_TOKEN --repo {repo}', "
                "or set ANTHROPIC_API_KEY",
            )
        )
    perms = _gh("api", f"repos/{repo}/actions/permissions/workflow")
    if perms is not None:
        if json.loads(perms).get("can_approve_pull_request_reviews"):
            findings.append((True, "GitHub Actions may create pull requests"))
        else:
            findings.append(
                (
                    False,
                    "Allow GitHub Actions to create pull requests: Settings > Actions > General > "
                    "Workflow permissions, or run: gh api -X PUT "
                    f"repos/{repo}/actions/permissions/workflow "
                    "-F can_approve_pull_request_reviews=true",
                )
            )
    for login in (cfg or {}).get("notify") or []:
        role = (
            _gh("api", f"repos/{repo}/collaborators/{login}/permission", "-q", ".permission") or ""
        ).strip()
        if role in ("admin", "maintain", "write"):
            findings.append((True, f"notify: @{login} has write access"))
        else:
            findings.append(
                (
                    False,
                    f"notify: @{login} has no write access to {repo}; NexKit still mentions "
                    "them, but they cannot run commands",
                )
            )
        branch = (_gh("api", f"repos/{repo}", "-q", ".default_branch") or "").strip()
    if branch:
        on_default = _gh("api", f"repos/{repo}/contents/{WORKFLOW_PATH}?ref={branch}", "-q", ".sha")
        findings.append(
            (
                bool(on_default),
                f"{WORKFLOW_PATH} is on {branch}"
                if on_default
                else f"Commit {WORKFLOW_PATH} and the config to {branch}; comment events only "
                "run workflows from the default branch",
            )
        )
    return findings
