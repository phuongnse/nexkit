"""Survey and install from actual user decisions; there are no project presets."""

from __future__ import annotations

import difflib
from pathlib import Path

from . import __version__
from .common import (
    Blocked,
    consumer_path,
    file_hash,
    kit_root,
    read_json,
    run,
    safe_path,
    write_json,
)
from .policy import agent_runner, authentication, clarification_limits, config, require

HOSTS = {
    "codex": ".agents/skills",
    "claude": ".claude/skills",
    "copilot": ".github/skills",
    "gemini": ".gemini/skills",
    "cursor": ".cursor/skills",
    "antigravity": ".agents/skills",
}


def survey(root):
    root = Path(root).resolve()
    listed = run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=root)
    files = sorted(set(listed.stdout.splitlines()))
    candidates = [
        p
        for p in files
        if Path(p).name
        in {
            "AGENTS.md",
            "CLAUDE.md",
            "README.md",
            "package.json",
            "pyproject.toml",
            "Makefile",
            "Cargo.toml",
            "go.mod",
            "justfile",
            "CMakeLists.txt",
        }
        or p.startswith(".github/workflows/")
    ]
    context = {}
    for p in candidates[:30]:
        path = root / p
        if path.is_file() and not path.is_symlink() and path.stat().st_size < 64000:
            context[p] = path.read_text(errors="replace")[:10000]
    remote = run(["git", "remote", "get-url", "origin"], cwd=root, check=False)
    return {
        "root": str(root),
        "files": files[:500],
        "file_count": len(files),
        "origin": remote.stdout.strip(),
        "context": context,
        "config": read_json(consumer_path(root, ".nexkit/project.json"))
        if (root / ".nexkit/project.json").exists()
        else None,
    }


def caller(cfg, workflow):
    pin = cfg["kit"]
    common = f"{pin['repository']}/.github/workflows/{workflow}.yml@{pin['ref']}"
    if workflow == "delivery":
        trigger = """  issue_comment:
    types: [created, edited, deleted]
  issues:
    types: [edited, closed]
  workflow_dispatch:
    inputs:
      issue:
        description: GitHub work item number
        required: true
        type: string
"""
    elif workflow in ("release", "clarify"):
        trigger = """  issue_comment:
    types: [created, edited, deleted]
  workflow_dispatch:
    inputs:
      issue:
        description: Release candidate issue number
        required: true
        type: string
"""
    else:
        trigger = """  workflow_dispatch:
    inputs:
      operation:
        description: request or release
        required: true
        type: choice
        options: [request, release]
      payload:
        description: JSON produced by the NexKit CLI
        required: true
        type: string
"""
    secrets = ""
    if workflow in ("delivery", "clarify") and authentication(cfg) == "api-key":
        secrets = "    secrets:\n      OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}\n"
    return f"""# Managed by NexKit {__version__}; update through nexkit install.
name: NexKit {workflow}
on:
{trigger}permissions: {{}}
jobs:
  nexkit:
    if: ${{{{ github.event_name == 'workflow_dispatch' || !github.event.issue.pull_request }}}}
    uses: {common}
    permissions:
      contents: write
      issues: write
      pull-requests: write
      checks: write
      actions: write
    with:
      kit_repository: {pin["repository"]}
      kit_ref: {pin["ref"]}
{secrets}\
"""


def managed_files(cfg, hosts):
    payload = {}
    for host in hosts:
        require(host in HOSTS, f"Unknown host {host}")
        for path in sorted((kit_root() / "plugins/nexkit/skills").rglob("*")):
            if path.is_file():
                rel = path.relative_to(kit_root() / "plugins/nexkit/skills")
                payload[f"{HOSTS[host]}/{rel.as_posix()}"] = path.read_bytes()
    for workflow in ("delivery", "release", "intake", "clarify"):
        payload[f".github/workflows/nexkit-{workflow}.yml"] = caller(cfg, workflow).encode()
    return payload


def installation_plan(root, cfg, hosts):
    root = Path(root).resolve()
    config(cfg)
    ledger_path = consumer_path(root, ".nexkit/installation.json")
    consumer_path(root, ".nexkit/project.json")
    ledger = read_json(ledger_path) if ledger_path.exists() else {"files": {}}
    files = managed_files(cfg, hosts)
    changes = []
    for name, content in files.items():
        safe_path(name)
        path = root / name
        require(not path.is_symlink(), f"Managed path is a symlink: {name}")
        for parent in path.parents:
            if parent == root:
                break
            require(not parent.is_symlink(), f"Managed directory is a symlink: {parent}")
        before = path.read_bytes() if path.exists() else b""
        if path.exists() and before != content:
            require(
                name in ledger["files"] and file_hash(path) == ledger["files"][name],
                f"Consumer edits preserved: {name}. Reconcile the diff before updating",
            )
        if before != content:
            changes.append(
                {
                    "path": name,
                    "diff": "".join(
                        difflib.unified_diff(
                            before.decode(errors="replace").splitlines(True),
                            content.decode(errors="replace").splitlines(True),
                            fromfile=name,
                            tofile=name,
                        )
                    ),
                }
            )
    return files, ledger, changes


def install(root, cfg, hosts, *, apply=False):
    root = Path(root).resolve()
    files, ledger, changes = installation_plan(root, cfg, hosts)
    if apply:
        # All conflicts are detected before writing any managed file.
        for name, content in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            ledger["files"][name] = file_hash(path)
        ledger.update(version=__version__, hosts=sorted(set(ledger.get("hosts", []) + hosts)))
        write_json(root / ".nexkit/installation.json", ledger)
    return {
        "applied": apply,
        "version": __version__,
        "changes": changes,
        "ownership": "Only recorded kit files are managed. Config, knowledge and code belong to the consumer.",
    }


def uninstall(root):
    root = Path(root).resolve()
    path = consumer_path(root, ".nexkit/installation.json")
    ledger = read_json(path)
    removed, preserved = [], []
    skill_root = kit_root() / "plugins/nexkit/skills"
    owned = {
        f"{prefix}/{p.relative_to(skill_root).as_posix()}"
        for prefix in HOSTS.values()
        for p in skill_root.rglob("*")
        if p.is_file()
    }
    owned.update(
        f".github/workflows/nexkit-{w}.yml" for w in ("delivery", "release", "intake", "clarify")
    )
    for name, previous in ledger["files"].items():
        safe_path(name)
        target = root / name
        parents_safe = not any(
            p.is_symlink() for p in target.parents if p != root and root in p.parents
        )
        if name not in owned or not parents_safe or not target.resolve().is_relative_to(root):
            preserved.append(name)
        elif target.is_file() and not target.is_symlink() and file_hash(target) == previous:
            target.unlink()
            removed.append(name)
        elif target.exists() or target.is_symlink():
            preserved.append(name)
    path.unlink()
    return {
        "removed": removed,
        "preserved": preserved,
        "consumer_data": "Config, knowledge, source and GitHub work items were preserved",
    }


def doctor(root, cfg, *, online=False, checks=False):
    from .checks import verify
    from .github import GitHub

    config(cfg)
    problems, verified = [], []
    if cfg["kit"]["version"] != __version__:
        problems.append("Local kit version differs from the CI kit version")
    ledger_path = consumer_path(root, ".nexkit/installation.json")
    if not ledger_path.exists():
        problems.append("NexKit has not been installed in this repository")
    else:
        ledger = read_json(ledger_path)
        for name, expected in ledger["files"].items():
            path = Path(root, name)
            if not path.is_file() or path.is_symlink() or file_hash(path) != expected:
                problems.append(f"Managed file missing/modified: {name}")
        verified.append("managed files inspected")
    verification = None
    if checks:
        verification = verify(cfg, root)
        if not verification["passed"]:
            problems.append("Consumer verification failed or is not yet possible")
            if cfg["application"] == "absent":
                problems.append("Initial repository has no verified application behavior")
        else:
            verified.append("consumer checks and E2E executed")
    else:
        problems.append("Consumer checks were not executed (use --checks)")
    if online:
        gh = GitHub(cfg["repository"])
        try:
            repository = gh.repo()
            require(repository["default_branch"] == cfg["default_branch"], "Default branch drift")
            gh.audit_settings(cfg["default_branch"])
            policy = gh.api(f"{gh.root}/actions/permissions/workflow")
            require(
                policy.get("can_approve_pull_request_reviews") is True,
                "Actions permission to create pull requests is disabled",
            )
            if authentication(cfg) == "api-key":
                secrets = gh.api(f"{gh.root}/actions/secrets")["secrets"]
                require(
                    any(x["name"] == "OPENAI_API_KEY" for x in secrets),
                    "OPENAI_API_KEY Actions secret is missing (an org secret may require admin verification)",
                )
                verified.append("API secret metadata inspected")
            if isinstance(agent_runner(cfg), list):
                runners = gh.api(
                    f"{gh.root}/actions/runners?per_page=100", pages=True, collection="runners"
                )
                require(
                    any(
                        r.get("status") == "online"
                        and set(agent_runner(cfg))
                        <= {label["name"].lower() for label in r.get("labels", [])}
                        for r in runners
                    ),
                    "No online runner matches this project's agent_runner labels",
                )
                verified.append(
                    "project runner registration inspected; login and isolation require a live probe"
                )
            verified.append("GitHub settings inspected")
        except Blocked as exc:
            problems.append(str(exc))
    else:
        problems.append("GitHub permissions/settings/authentication have not been verified")
    return {
        "ready": not problems,
        "ready_scope": "configuration and declared checks; live model access is reported separately",
        "verified": verified,
        "problems": problems,
        "verification": verification,
        "engine": cfg["engine"],
        "models": cfg["models"],
        "reasoning_effort": cfg.get("reasoning_effort", {}),
        "authentication": authentication(cfg),
        "agent_runner": agent_runner(cfg),
        "limits": cfg["limits"],
        "clarification": clarification_limits(cfg),
        "live_agent_verified": False,
    }
