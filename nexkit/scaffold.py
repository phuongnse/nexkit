"""`nexkit init` and `nexkit doctor`: install and check a repository's NexKit setup."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from importlib import resources
from pathlib import Path

from . import __version__, app
from . import config as configuration
from .runtimes import DECLARED_SECRETS, PROVIDERS

KIT_REPOSITORY = "phuongnse/nexkit"
# How the key of an `actions/cache` step that saves an entry by hand starts.
HAND_WRITTEN_KEY = "nexkit-${{ runner.os }}-"
WORKFLOW_PATH = ".github/workflows/nexkit.yml"


# What the template holds in place of the `workflow_run` trigger, and what replaces it when
# `nexkit init` knows the names of the workflows that sweeps watch (merge.after_workflows,
# autopilot.watch_workflows).
WORKFLOW_RUN_COMMENT = (
    "  # workflow_run:\n"
    "  #   workflows: [CI]\n"
    "  #   types: [completed]\n"
    "  #   branches: [__DEFAULT_BRANCH__]\n"
)
WORKFLOW_RUN = (
    "  workflow_run:\n"
    "    workflows: [{names}]\n"
    "    types: [completed]\n"
    "    branches: [__DEFAULT_BRANCH__]\n"
)


def workflow_text(kit_repo, kit_ref, default_branch="main", after_names=()):
    template = resources.files("nexkit").joinpath("templates/workflow.yml").read_text()
    if after_names:
        names = ", ".join(json.dumps(name) for name in after_names)
        template = template.replace(WORKFLOW_RUN_COMMENT, WORKFLOW_RUN.format(names=names))
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


def build_checks(checks, *, timeouts=(), slow=(), results=(), groups=(), unaffected=()):
    """`checks` from the `--check` values, with the per-check flags applied: `timeouts`
    (NAME=MINUTES), `slow` (NAME), `results` (NAME=PATH, one path each), `groups`
    (NAME=GROUP) and `unaffected` (NAME=GLOB, one pattern each, in order). The
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
    for value in groups:
        name, group = _pair("--check-group", "NAME=GROUP", value)
        _check_named(built, "--check-group", name)["group"] = group
    for value in unaffected:
        name, pattern = _pair("--check-unaffected", "NAME=GLOB", value)
        check = _check_named(built, "--check-unaffected", name)
        check.setdefault("unaffected_by", []).append(pattern)
    return built


def refuse_existing(root):
    """Refuse to overwrite NexKit's files; `nexkit init` asks before it creates an App."""
    root = Path(root)
    paths = (root / configuration.CONFIG_PATH, root / WORKFLOW_PATH)
    existing = [str(p.relative_to(root)) for p in paths if p.exists()]
    if existing:
        raise FileExistsError(f"Already exists: {', '.join(existing)} (use --force)")


def configured_app(root):
    """The `github_app` of the configuration in `root`, or None."""
    try:
        raw = json.loads((Path(root) / configuration.CONFIG_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    found = raw.get("github_app") if isinstance(raw, dict) else None
    return found if isinstance(found, dict) and found.get("slug") else None


def github_app(root, *, slug=None, app_id=None, key_file=None, name=None):
    """(the configuration's `github_app`, lines for the person). `slug` names an App whose
    secrets are set already, as an organization's can be; `app_id` and `key_file` an App
    that exists, which must be installed here, and set its secrets; otherwise a repository
    with an App keeps it, and any other creates one with the manifest flow."""
    if bool(app_id) != bool(key_file):
        raise app.AppError("--app-id and --app-private-key go together")
    if slug:
        return app.settings(slug), []
    known = configured_app(root)
    if known and not app_id:
        return known, []
    repo = repository_of(root)
    if not repo:
        raise app.AppError(
            "Cannot tell the GitHub repository from the 'origin' remote, which the App needs; "
            "add it, or pass --app-slug SLUG for an App that exists"
        )
    if app_id:
        slug, lacking = app.use_existing(repo, app_id, key_file)
        lines = [f"Set the secrets of the App {slug}, which is installed on {repo}."]
        if lacking:
            lines.append(f"It lacks {', '.join(lacking)}; 'nexkit doctor' says how to grant it.")
        return app.settings(slug), lines
    kind = (_gh("api", f"repos/{repo}", "-q", ".owner.type") or "").strip()
    slug = app.create(repo, kind == "Organization", gh_token(), name=name)
    return app.settings(slug), [
        f"Created the App {slug} and set its secrets. Install it on {repo}, and on every other "
        f"repository of this owner that uses NexKit: {app.install_url(slug)}"
    ]


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
    check_groups=(),
    check_unaffected=(),
    parallel_checks=False,
    notify=(),
    evidence=(),
    after_workflows=(),
    watch_workflows=(),
    github_app=None,
):
    root = Path(root)
    config_path = root / configuration.CONFIG_PATH
    workflow_path = root / WORKFLOW_PATH
    if not force:
        refuse_existing(root)
    raw = {
        "version": 1,
        "agents": {"claude": dict(configuration.CLAUDE_AGENT)},
        "agent": "claude",
        "model": model,
        "setup": setup,
        "checks": build_checks(
            checks,
            timeouts=check_timeouts,
            slow=check_slow,
            results=check_results,
            groups=check_groups,
            unaffected=check_unaffected,
        ),
        "fix": dict(configuration.DEFAULTS["fix"]),
        "merge": {"auto": False},
    }
    if github_app:
        raw["github_app"] = github_app
    if parallel_checks:
        raw["parallel_checks"] = True
    if notify:
        raw["notify"] = list(dict.fromkeys(login.lstrip("@") for login in notify))
    if evidence:
        raw["evidence"] = {"artifacts": list(dict.fromkeys(evidence))}
    after_workflows = list(dict.fromkeys(after_workflows))
    if after_workflows:
        raw["merge"]["after_workflows"] = after_workflows
    watch_workflows = list(dict.fromkeys(watch_workflows))
    if watch_workflows:
        raw["autopilot"] = {"watch_workflows": watch_workflows}
    configuration.validate(raw)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(raw, indent=2) + "\n")
    workflow_path.parent.mkdir(parents=True, exist_ok=True)
    names = [
        workflow_name(root, name) or f".github/workflows/{name}"
        for name in [*after_workflows, *watch_workflows]
    ]
    workflow_path.write_text(
        workflow_text(kit_repo, kit_ref or f"v{__version__}", default_branch_of(root), names)
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


def _watched_workflow(root, name):
    """Whether something runs a workflow of autopilot.watch_workflows on the default
    branch: a schedule, a push, or a person's dispatch."""
    path = root / ".github/workflows" / name
    if not path.is_file():
        return (False, f"autopilot.watch_workflows: {path.relative_to(root)} does not exist")
    text = _uncommented(path.read_text(errors="replace"))
    if not re.search(r"\b(?:schedule|push|workflow_dispatch)\b", text):
        return (
            False,
            f"autopilot.watch_workflows: {name} has no schedule, push or workflow_dispatch "
            "trigger, so nothing runs it on the default branch for autopilot to watch",
        )
    return (True, f"autopilot.watch_workflows: autopilot watches the runs of {name}")


def _trusted_workflow(root, name):
    """Whether a workflow of autopilot.trusted_workflows exists."""
    path = root / ".github/workflows" / name
    if not path.is_file():
        return (False, f"autopilot.trusted_workflows: {path.relative_to(root)} does not exist")
    return (True, f"autopilot.trusted_workflows: autopilot takes the issues {name} opens")


def workflow_name(root, name):
    """The name GitHub gives the workflow file `name`: its `name:`, else its path. None
    when the file does not exist."""
    path = root / ".github/workflows" / name
    if not path.is_file():
        return None
    text = _uncommented(path.read_text(errors="replace"))
    match = re.search(r"^name:[ \t]*(\S.*?)[ \t]*$", text, re.M)
    if not match:
        return f".github/workflows/{name}"
    return match.group(1).strip("'\"")


def _cache_action(root, cfg, kit_repo):
    """Findings: with `cache` set, a workflow of the default branch saves the caches, since
    NexKit only restores them. Hand-written steps that hold NexKit's key can work, so they
    are information; with neither, every run starts cold."""
    if not cfg["cache"]:
        return []
    folder = root / ".github/workflows"
    paths = sorted(folder.glob("*.y*ml")) if folder.is_dir() else []
    texts = [_uncommented(path.read_text(errors="replace")) for path in paths]
    action = f"{kit_repo}/cache@"
    if any(re.search(rf"^\s*(-\s*)?uses:\s*{re.escape(action)}", t, re.M) for t in texts):
        return [(True, "A workflow saves the caches with NexKit's cache action")]
    use = f"use '{action}<ref>' in a workflow of the default branch (docs/configuration.md#caches)"
    if any(HAND_WRITTEN_KEY in text for text in texts):
        return [
            (
                True,
                "A workflow saves the caches with steps of its own, which must stay in step "
                f"with `cache` by hand; to state them once, {use}",
            )
        ]
    return [(False, f"cache is set, but no workflow saves the caches, so runs start cold: {use}")]


def _ci_trigger(root, cfg, workflow):
    """A finding: with autopilot, a finished run of each workflow that sweeps watch
    (merge.after_workflows, autopilot.watch_workflows) starts a sweep at once, through a
    `workflow_run` trigger that names it, which the 'nexkit' job lets through."""
    watched = configuration.watched_workflows(cfg)
    names = [n for n in (workflow_name(root, f) for f in watched) if n]
    if not names:
        return []
    found = trigger(workflow, "workflow_run")
    missing = [
        name
        for name in names
        if found is None or not re.search(rf"[\[\s,'\"]{re.escape(name)}[\]\s,'\"]", found)
    ]
    later = (
        "so their failures on the default branch wait for the end of the next NexKit run or "
        "the hourly schedule"
    )
    if missing:
        listed = ", ".join(f"'{name}'" for name in missing)
        return [
            (
                False,
                f"autopilot is on, but the 'workflow_run' trigger in {WORKFLOW_PATH} does not "
                f"name {listed} of {_watched_settings(cfg)}, {later}; add it as "
                "docs/configuration.md#autopilot shows",
            )
        ]
    if "types" in found and not re.search(r"\bcompleted\b", found):
        return [
            (
                False,
                f"The 'workflow_run' trigger in {WORKFLOW_PATH} does not listen to "
                f"'completed', {later}",
            )
        ]
    text = _uncommented(workflow)
    jobs = text.split("\njobs:", 1)[1] if "\njobs:" in text else ""
    if '"workflow_run"' not in jobs:
        return [
            (
                False,
                f"The 'nexkit' job in {WORKFLOW_PATH} does not let 'workflow_run' events "
                f"through its 'if', {later}; add \"workflow_run\" as 'nexkit init' writes it",
            )
        ]
    return [
        (
            True,
            f"Finished runs of {_watched_settings(cfg)} on the default branch start sweeps",
        )
    ]


def _watched_settings(cfg):
    """The settings that list the workflows that sweeps watch, as findings name them."""
    return " and ".join(
        key
        for key, value in (
            ("merge.after_workflows", cfg["merge"]["after_workflows"]),
            ("autopilot.watch_workflows", cfg["autopilot"]["watch_workflows"]),
        )
        if value
    )


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


def _triggers(root, cfg, workflow):
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
        findings += _ci_trigger(root, cfg, workflow)
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
    # With `null` every open issue is in scope, so milestone events are not needed. Those
    # that `nexkit init` writes still start a sweep, as every event in the group does.
    by_milestone = cfg["autopilot"]["scope"] == "milestone"
    needed = ["opened", "reopened", "closed", *(["milestoned"] if by_milestone else [])]
    issues = trigger(workflow, "issues")
    # Whole words: `reopened` holds `opened`.
    missing = [
        t
        for t in needed
        if issues is None or ("types" in issues and not re.search(rf"\b{t}\b", issues))
    ]
    if by_milestone and trigger(workflow, "milestone") is None:
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
    for key in configuration.APP_SECRETS:
        if cfg["github_app"][key]:
            needed.append((f"github_app.{key}", (cfg["github_app"][key],)))
    declared = {*DECLARED_SECRETS, *configuration.APP_SECRETS.values()}
    for name, secrets in needed:
        if any(re.search(rf"\bsecrets\.{secret}\b", text) for secret in secrets):
            continue
        how = "add it under 'secrets:'" if set(secrets) <= declared else "write 'secrets: inherit'"
        who = name if name.startswith("github_app.") else f"The `{name}` agent"
        findings.append(
            (
                False,
                f"{who} needs {' or '.join(secrets)}, but {WORKFLOW_PATH} does not "
                f"pass it to the pipeline; {how} as 'nexkit init' writes it",
            )
        )
    return findings


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
    return findings


def _app_secrets(cfg, names, repo):
    """Findings: the secrets of `github_app` are set, for the repository or its owner."""
    findings = []
    for key in configuration.APP_SECRETS:
        name = cfg["github_app"][key]
        if name in names:
            findings.append((True, f"github_app.{key}: {name} is set"))
        else:
            findings.append(
                (
                    False,
                    f"github_app.{key} names {name}, which is not set: run 'gh secret set "
                    f"{name} --repo {repo}', or 'nexkit init --force --app-id ID "
                    "--app-private-key FILE', which sets both",
                )
            )
    return findings


def _app_on_github(cfg, repo, key_file=None, app_id=None):
    """Findings: the App has NexKit's permissions and is installed on `repo`. GitHub shows a
    private App, which `nexkit init` creates, only to the App itself, and only the App may
    ask where it is installed, so both take its id and private key. What doctor cannot tell
    is no failure."""
    slug = cfg["github_app"]["slug"]
    owner = repo.split("/")[0]
    kind = (_gh("api", f"repos/{repo}", "-q", ".owner.type") or "").strip()
    where = app.settings_url(slug, owner, kind == "Organization")
    if not (key_file and app_id):
        found = _gh("api", f"apps/{slug}")
        hint = "pass --app-id ID --app-private-key FILE to check"
        if found is None:
            return [
                (
                    True,
                    f"Could not read the App {slug}: GitHub shows a private App to no person's "
                    f"token; {hint} it and its installation on {repo}",
                )
            ]
        findings = [_granted(slug, json.loads(found).get("permissions"), where)]
        findings.append((True, f"Could not tell whether {slug} is installed on {repo}; {hint}"))
        return findings
    try:
        state = app.inspect(repo, app_id, key_file)
    except app.AppError as exc:
        return [(False, str(exc))]
    if state["slug"] != slug:
        return [
            (
                False,
                f"The key and id belong to the App {state['slug']}, but github_app.slug is "
                f"{slug}; correct one of them",
            )
        ]
    findings = [_granted(slug, state["granted"], where)]
    if not state["installed"]:
        how = f"install it at {app.install_url(slug)}"
        return [*findings, (False, f"The App {slug} is not installed on {repo}: {how}")]
    findings.append((True, f"The App {slug} is installed on {repo}"))
    lacking = app.missing(state["permissions"])
    if lacking:
        accept = f"https://github.com/{owner}/{repo.split('/')[1]}/settings/installations"
        findings.append(
            (
                False,
                f"The App's installation on {repo} lacks {', '.join(lacking)}: accept the "
                f"App's new permissions at {accept}",
            )
        )
    return findings


def _granted(slug, granted, where):
    lacking = app.missing(granted)
    if lacking:
        return (
            False,
            f"The App {slug} lacks {', '.join(lacking)}: grant it at {where}, then accept "
            "the change on the installation",
        )
    return (True, f"The App {slug} has the permissions NexKit needs")


def doctor(root, key_file=None, app_id=None):
    """Return a list of (ok, message) findings. With `key_file` and `app_id`, the App's
    private key and id, it also asks GitHub as the App about it and its installation."""
    root = Path(root)
    findings = []
    cfg = None
    try:
        cfg = configuration.load(root)
        findings.append((True, f"{configuration.CONFIG_PATH} is valid"))
        if not cfg["checks"]:
            findings.append((False, "No checks configured; NexKit cannot verify changes"))
        findings += [
            _after_merge_workflow(root, name) for name in configuration.after_workflow_names(cfg)
        ]
        findings += [_watched_workflow(root, n) for n in cfg["autopilot"]["watch_workflows"]]
        findings += [_trusted_workflow(root, n) for n in cfg["autopilot"]["trusted_workflows"]]
        problem = configuration.app_problem(cfg)
        if problem:
            findings.append((False, problem))
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
            findings += _triggers(root, cfg, text)
            findings += _passed_secrets(cfg, text)
            findings += _cache_action(root, cfg, pinned[0] if pinned else KIT_REPOSITORY)

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
    # An organization can hold secrets once for all its repositories.
    names |= set(
        (
            _gh("api", f"repos/{repo}/actions/organization-secrets", "-q", ".secrets[].name") or ""
        ).split()
    )
    if cfg:
        findings += _secrets_set(cfg, names, repo)
    if cfg and not configuration.app_problem(cfg):
        findings += _app_secrets(cfg, names, repo)
        findings += _app_on_github(cfg, repo, key_file, app_id)
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
