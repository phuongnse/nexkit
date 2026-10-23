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
from .runtimes import DECLARED_SECRETS, PROVIDERS

KIT_REPOSITORY = "phuongnse/nexkit"
WORKFLOW_PATH = ".github/workflows/nexkit.yml"


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
    """The default branch on GitHub, else of `origin` in the clone, else the current
    branch, else `main`."""
    repo = repository_of(root)
    name = (_gh("api", f"repos/{repo}", "-q", ".default_branch") or "").strip() if repo else ""
    if re.fullmatch(r"[\w./-]+", name):
        return name
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


def _check_named(checks, flag, name):
    for check in checks:
        if check["name"] == name.strip():
            return check
    raise configuration.ConfigError(f"{flag} names '{name.strip()}', which no --check defines")


def _pair(flag, shape, value):
    name, sep, rest = value.partition("=")
    if not sep or not name.strip() or not rest.strip():
        raise configuration.ConfigError(f"{flag} must look like {shape}, got '{value}'")
    return name, rest.strip()


def build_checks(checks, *, timeouts=(), slow=(), results=()):
    """`checks` from the `--check` values, with the per-check flags applied: `timeouts`
    (NAME=MINUTES), `slow` (NAME) and `results` (NAME=PATH, one path each). The
    configuration's own validation checks the values."""
    built = [parse_check(c) for c in checks]
    for value in timeouts:
        name, minutes = _pair("--check-timeout", "NAME=MINUTES", value)
        if not minutes.isdigit():
            raise configuration.ConfigError(f"--check-timeout must give whole minutes: '{value}'")
        _check_named(built, "--check-timeout", name)["timeout_minutes"] = int(minutes)
    for name in slow:
        _check_named(built, "--check-slow", name)["slow"] = True
    for value in results:
        name, path = _pair("--check-results", "NAME=PATH", value)
        _check_named(built, "--check-results", name).setdefault("results", []).append(path)
    return built


def init(
    root,
    *,
    checks,
    setup,
    model,
    kit_repo,
    kit_ref,
    force,
    check_timeouts=(),
    check_slow=(),
    check_results=(),
    notify=(),
    evidence=(),
):
    root = Path(root)
    config_path = root / configuration.CONFIG_PATH
    workflow_path = root / WORKFLOW_PATH
    if not force:
        existing = [str(p.relative_to(root)) for p in (config_path, workflow_path) if p.exists()]
        if existing:
            raise FileExistsError(f"Already exists: {', '.join(existing)} (use --force)")
    raw = {
        "version": 1,
        "agents": {"claude": dict(configuration.CLAUDE_AGENT)},
        "agent": "claude",
        "model": model,
        "setup": setup,
        "checks": build_checks(
            checks, timeouts=check_timeouts, slow=check_slow, results=check_results
        ),
        "fix": dict(configuration.DEFAULTS["fix"]),
        "merge": {"auto": False},
    }
    if notify:
        raw["notify"] = list(dict.fromkeys(login.lstrip("@") for login in notify))
    if evidence:
        raw["evidence"] = {"artifacts": list(dict.fromkeys(evidence))}
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


def gh_token():
    """The GitHub CLI's token, or None."""
    return (_gh("auth", "token") or "").strip() or None


def repository_of(root):
    proc = subprocess.run(
        ["git", "remote", "get-url", "origin"], cwd=root, capture_output=True, text=True
    )
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", proc.stdout.strip())
    return match.group(1) if match else None


def _uncommented(text):
    return re.sub(r"(^|\s)#.*", "", text)


def _after_merge_workflow(root, name):
    """Whether NexKit can start a workflow of merge.after_workflows with a dispatch."""
    path = root / ".github/workflows" / name
    if not path.is_file():
        return (False, f"merge.after_workflows: {path.relative_to(root)} does not exist")
    text = _uncommented(path.read_text(errors="replace"))
    if not re.search(r"\bworkflow_dispatch\b", text):
        return (
            False,
            f"merge.after_workflows: {name} has no workflow_dispatch trigger, "
            "so NexKit cannot start it after a merge",
        )
    return (True, f"merge.after_workflows: {name} can be started after a merge")


def pipeline_ref(workflow):
    """(NexKit repository, ref) of the pipeline the workflow calls, or None."""
    match = re.search(
        r"uses:\s*(\S+)/\.github/workflows/pipeline\.yml@(\S+)", _uncommented(workflow)
    )
    return (match.group(1), match.group(2)) if match else None


def _same_ref(workflow, ref):
    """Findings: `nexkit_ref` names the ref the pipeline is called at. The jobs check out
    NexKit's code at `nexkit_ref`, so with another ref they run another release's code."""
    match = re.search(r"^\s*nexkit_ref:\s*['\"]?([^'\"\s]+)", _uncommented(workflow), re.M)
    if match and match.group(1) == ref:
        return []
    found = f"is {match.group(1)}" if match else "is not set"
    return [
        (
            False,
            f"{WORKFLOW_PATH} calls the pipeline at {ref}, but nexkit_ref {found}; set both "
            "to the same tag",
        )
    ]


def _version(ref):
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", ref or "")
    return tuple(int(part) for part in match.groups()) if match else None


def _newer_release(kit_repo, ref):
    """Findings: a NexKit release newer than the pinned tag. Staying on a release is a
    choice, so this is information, never a problem. A branch or commit says nothing."""
    pinned = _version(ref)
    latest = (_gh("api", f"repos/{kit_repo}/releases/latest", "-q", ".tag_name") or "").strip()
    if not pinned or not _version(latest) or _version(latest) <= pinned:
        return []
    return [
        (
            True,
            f"NexKit {latest} is available; this repository uses {ref}. To upgrade, see "
            f"https://github.com/{kit_repo}/releases/tag/{latest}",
        )
    ]


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


LEVELS = {"none": 0, "read": 1, "write": 2}


def granted(workflow):
    """The token permissions the workflow gives its job: the job's own block, else the
    workflow's. `read-all` and `write-all` give every permission, under the name `*`."""
    text = _uncommented(workflow)
    job = text.split("\njobs:", 1)[1] if "\njobs:" in text else ""
    for found in (
        re.search(r"^    permissions:[ \t]*(\S*)[ \t]*\n((?:      .*\n|[ \t]*\n)*)", job, re.M),
        re.search(r"^permissions:[ \t]*(\S*)[ \t]*\n((?:  .*\n|[ \t]*\n)*)", text, re.M),
    ):
        if not found:
            continue
        if found.group(1) in ("read-all", "write-all"):
            return {"*": found.group(1).removesuffix("-all")}
        return dict(re.findall(r"^\s+([a-z-]+):\s*(read|write|none)\b", found.group(2), re.M))
    return {}


def _permissions(workflow):
    """A finding: the workflow gives the pipeline every permission that 'nexkit init'
    writes. GitHub does not start a workflow whose called jobs ask for more than it gives."""
    have = granted(workflow)
    needed = granted(workflow_text(KIT_REPOSITORY, "main"))
    missing = [
        f"{name}: {level}"
        for name, level in needed.items()
        if LEVELS[have.get(name, have.get("*", "none"))] < LEVELS[level]
    ]
    if missing:
        return (
            False,
            f"{WORKFLOW_PATH} does not give the pipeline {', '.join(missing)}, so GitHub "
            "starts none of its runs; add it under 'permissions:' as 'nexkit init' writes it",
        )
    return (True, "The workflow gives the pipeline the permissions its jobs need")


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
    if cfg["close_parent_issues"]:
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
    if cfg["conflicts"]["auto_resolve"]:
        if trigger(workflow, "push") is None:
            findings.append(
                (
                    False,
                    f"conflicts.auto_resolve is on, but {WORKFLOW_PATH} has no 'push' trigger "
                    "for the default branch, so merges by people are noticed only by the "
                    "hourly schedule; add it as 'nexkit init' writes it",
                )
            )
        else:
            findings.append((True, "Pushes to the default branch start the conflict check"))
    if cfg["autopilot"]["enabled"]:
        findings.append(_autopilot_triggers(cfg, workflow))
    if cfg["usage_limit"]["resume"]:
        if trigger(workflow, "schedule") is None:
            findings.append(
                (
                    False,
                    f"{WORKFLOW_PATH} has no 'schedule' trigger, so runs paused at the agent's "
                    "usage limit cannot resume; add it as 'nexkit init' writes it, or set "
                    "usage_limit.resume to false",
                )
            )
        else:
            findings.append((True, "A schedule resumes runs paused at the agent's usage limit"))
    return findings


def _autopilot_triggers(cfg, workflow):
    """A finding: autopilot needs the events that bring issues into scope, and its own
    concurrency group so those events never replace a command that waits."""
    needed = ["opened", "reopened", "closed"]
    needed += ["labeled"] if cfg["autopilot"]["scope"] == "label" else ["milestoned"]
    issues = trigger(workflow, "issues")
    missing = [t for t in needed if issues is None or ("types" in issues and t not in issues)]
    if cfg["autopilot"]["scope"] == "milestone" and trigger(workflow, "milestone") is None:
        missing.append("milestone: closed")
    if trigger(workflow, "schedule") is None:
        missing.append("schedule")
    if missing:
        return (
            False,
            f"autopilot is on, but {WORKFLOW_PATH} does not listen to "
            f"{', '.join(f'{name!r}' for name in missing)}; add the triggers as 'nexkit init' "
            "writes them",
        )
    if "'autopilot'" not in _uncommented(workflow):
        return (
            False,
            f"autopilot is on, but the concurrency group in {WORKFLOW_PATH} does not give "
            "issue and milestone events the group 'autopilot', so they can replace a command "
            "that waits; use the group 'nexkit init' writes",
        )
    return (True, "Issue and milestone events start autopilot sweeps")


def _passed_secrets(cfg, workflow):
    """Findings: the workflow passes the pipeline a secret of each agent that a stage uses.
    With `secrets: inherit` it passes them all; one by one, only the declared ones."""
    findings = []
    text = _uncommented(workflow)
    if re.search(r"^\s+secrets:\s*inherit\s*$", text, re.M):
        return findings
    needed = list(configuration.credentials(cfg))
    push = cfg["publish"]["push_token_secret"]
    if push:
        needed.append(("publish", (push,)))
    for name, secrets in needed:
        if any(re.search(rf"\bsecrets\.{secret}\b", text) for secret in secrets):
            continue
        how = (
            "add it under 'secrets:'"
            if set(secrets) <= set(DECLARED_SECRETS)
            else "write 'secrets: inherit'"
        )
        who = "publish.push_token_secret" if name == "publish" else f"The `{name}` agent"
        findings.append(
            (
                False,
                f"{who} needs {' or '.join(secrets)}, but {WORKFLOW_PATH} does not "
                f"pass it to the pipeline; {how} as 'nexkit init' writes it",
            )
        )
    return findings


def _unattended_push(cfg):
    """Findings: whether work that NexKit starts by itself can push every merge of the base
    branch. The Actions token may not push changes to workflow files, so without a push
    token a round that merges such changes from the base branch waits for a person. Running
    without a push token is a choice, so this is information, never a problem."""
    if cfg["publish"]["push_token_secret"]:
        return []
    on = [
        key
        for key, enabled in (
            ("autopilot.enabled", cfg["autopilot"]["enabled"]),
            ("conflicts.auto_resolve", cfg["conflicts"]["auto_resolve"]),
        )
        if enabled
    ]
    if not on:
        return []
    return [
        (
            True,
            f"{' and '.join(on)} {'is' if len(on) == 1 else 'are'} on, but "
            "publish.push_token_secret is not set: a round that merges changes to "
            ".github/workflows/ from the base branch cannot push with the Actions token and "
            "waits for a person. Name a fine-grained token with Contents, Pull requests and "
            "Workflows write access (see the configuration reference, Secrets)",
        )
    ]


def _unheard(cfg):
    """Findings: whether anyone hears when autopilot stops for a person. Autopilot runs
    unattended, so a stop that mentions nobody looks like a stall."""
    if not cfg["autopilot"]["enabled"] or cfg["notify"]:
        return []
    return [
        (
            True,
            "autopilot.enabled is on, but notify is empty: nobody is mentioned when autopilot "
            'stops for a person. Name the logins to mention, such as "notify": ["owner"], or '
            "run 'nexkit init --notify LOGIN'",
        )
    ]


def _secrets_set(cfg, names, repo):
    """Findings: a secret of each agent that a stage uses is set in the repository."""
    findings = []
    for name, secrets in configuration.credentials(cfg):
        found = sorted(names & set(secrets))
        if found:
            findings.append((True, f"Secret for the `{name}` agent is set: {', '.join(found)}"))
            continue
        agent = configuration.agent(cfg, name)
        hint = PROVIDERS[agent["provider"]].secret_hint(secrets, repo)
        findings.append((False, f"Set a secret for the `{name}` agent: {hint}"))
    push = cfg["publish"]["push_token_secret"]
    if push and push not in names:
        findings.append(
            (
                False,
                f"publish.push_token_secret names {push}, which is not set: run "
                f"'gh secret set {push} --repo {repo}' with a fine-grained token, or remove it",
            )
        )
    elif push:
        findings.append((True, f"Pushes use the {push} secret"))
    elif "NEXKIT_PUSH_TOKEN" in names:
        findings.append(
            (
                False,
                "The NEXKIT_PUSH_TOKEN secret is set, but publish.push_token_secret does not "
                "name it, so NexKit pushes with the Actions token. To use it, add "
                '"publish": {"push_token_secret": "NEXKIT_PUSH_TOKEN"} to '
                f"{configuration.CONFIG_PATH}",
            )
        )
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
        findings += [_after_merge_workflow(root, name) for name in cfg["merge"]["after_workflows"]]
        findings += _unattended_push(cfg)
        findings += _unheard(cfg)
    except configuration.ConfigError as exc:
        findings.append((False, str(exc)))

    workflow = root / WORKFLOW_PATH
    pinned = None
    if not workflow.is_file():
        findings.append((False, f"Missing {WORKFLOW_PATH}; run 'nexkit init'"))
    else:
        text = workflow.read_text()
        pinned = pipeline_ref(text)
        if pinned:
            findings.append((True, f"Workflow uses {pinned[0]} pipeline at {pinned[1]}"))
            findings += _same_ref(text, pinned[1])
        else:
            findings.append((False, f"{WORKFLOW_PATH} does not call the NexKit pipeline"))
        findings.append(_concurrency(text))
        findings.append(_permissions(text))
        if cfg:
            findings += _triggers(cfg, text)
            findings += _passed_secrets(cfg, text)

    repo = repository_of(root)
    if not repo:
        findings.append((False, "The 'origin' remote is not a GitHub repository"))
        return findings
    if _gh("auth", "status") is None:
        findings.append((False, "GitHub CLI is missing or not signed in; skipped GitHub checks"))
        return findings

    if pinned:
        findings += _newer_release(*pinned)
    names = set(
        (_gh("secret", "list", "--repo", repo, "--json", "name", "-q", ".[].name") or "").split()
    )
    if cfg:
        findings += _secrets_set(cfg, names, repo)
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
    for login in (cfg or {}).get("notify", []):
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
    if branch and workflow.is_file():
        push = trigger(workflow.read_text(), "push")
        if push is not None and not re.search(rf"[\[\s,'\"]{re.escape(branch)}[\]\s,'\"]", push):
            findings.append(
                (
                    False,
                    f"The 'push' trigger in {WORKFLOW_PATH} does not list the default branch "
                    f"{branch}, so merges into it do not start the conflict check",
                )
            )
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
