"""Survey and install from actual user decisions; there are no project presets."""

from __future__ import annotations

import difflib
import hashlib
from pathlib import Path

from . import __version__
from .common import (
    Blocked,
    consumer_path,
    digest,
    file_hash,
    kit_root,
    read_json,
    run,
    safe_path,
    write_json,
)
from .pipelines import bundle_path, effective_config
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


def managed_files(cfg, hosts, *, root=None, bundle=None):
    payload = {}
    for host in hosts:
        require(host in HOSTS, f"Unknown host {host}")
        for path in sorted((kit_root() / "plugins/nexkit/skills").rglob("*")):
            if path.is_file():
                rel = path.relative_to(kit_root() / "plugins/nexkit/skills")
                payload[f"{HOSTS[host]}/{rel.as_posix()}"] = path.read_bytes()
    if cfg["schema"] == 1:
        require(bundle is None, "Workflow bundles require project schema 2")
        for workflow in ("delivery", "release", "intake", "clarify"):
            payload[f".github/workflows/nexkit-{workflow}.yml"] = caller(cfg, workflow).encode()
    else:
        for name, record in cfg["files"].items():
            # Accepted consumer files are inspected, never adopted implicitly.
            source = consumer_path(bundle if record["managed"] and bundle else root, name)
            require(source.is_file(), f"Accepted setup file is missing: {name}")
            content = source.read_bytes()
            require(
                hashlib.sha256(content).hexdigest() == record["sha256"],
                f"Setup file hash mismatch: {name}",
            )
            if record["managed"]:
                payload[name] = content
    return payload


def installation_plan(root, cfg, hosts, *, bundle=None):
    root = Path(root).resolve()
    config(cfg)
    ledger_path = consumer_path(root, ".nexkit/installation.json")
    project_path = consumer_path(root, ".nexkit/project.json")
    ledger = read_json(ledger_path) if ledger_path.exists() else {"files": {}}
    prior = read_json(project_path) if project_path.exists() else None
    if ledger.get("project"):
        require(
            prior is not None and digest(prior) == ledger["project"],
            "Installed project configuration was edited; reconcile it before applying a setup bundle",
        )
    files = managed_files(cfg, hosts, root=root, bundle=bundle)
    owned = {
        name
        for name, record in (prior or {}).get("files", {}).items()
        if record.get("managed") is True and name in ledger.get("bundle_files", [])
    }
    previous_bundle = set(ledger.get("bundle_files", []))
    if cfg["schema"] == 2 and prior and prior.get("schema") == 1:
        legacy = {
            f".github/workflows/nexkit-{name}.yml"
            for name in ("intake", "clarify", "delivery", "release")
        }
        previous_bundle.update(legacy & set(ledger["files"]))
        owned.update(legacy & set(ledger["files"]))
    if cfg["schema"] == 2:
        # The desired manifest is the full workflow set. Retire only previously
        # accepted managed files; skills for other installed hosts stay installed.
        obsolete = previous_bundle - set(cfg["files"])
        for name in obsolete:
            bundle_path(name)
            path = consumer_path(root, name)
            require(name in owned, f"Cannot establish prior setup ownership: {name}")
            if path.exists():
                require(
                    file_hash(path) == ledger["files"].get(name),
                    f"Consumer edits preserved: {name}. Reconcile the obsolete workflow before removing it",
                )
                files[name] = None
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
        after = content if content is not None else b""
        if path.exists() and before != after:
            require(
                name in ledger["files"] and file_hash(path) == ledger["files"][name],
                f"Consumer edits preserved: {name}. Reconcile the diff before updating",
            )
        if (
            before != after
            or (content is None and path.exists())
            or (content is not None and not path.exists())
        ):
            changes.append(
                {
                    "path": name,
                    "operation": "remove"
                    if content is None
                    else "update"
                    if path.exists()
                    else "add",
                    "diff": "".join(
                        difflib.unified_diff(
                            before.decode(errors="replace").splitlines(True),
                            after.decode(errors="replace").splitlines(True),
                            fromfile=name,
                            tofile=name,
                        )
                    ),
                }
            )
        elif cfg["schema"] == 2 and name in cfg["files"] and name not in ledger["files"]:
            changes.append({"path": name, "operation": "adopt", "diff": ""})
    if cfg["schema"] == 2 and prior != cfg:
        from .common import canonical

        changes.append(
            {
                "path": ".nexkit/project.json",
                "operation": "update" if prior else "add",
                "diff": "".join(
                    difflib.unified_diff(
                        ((canonical(prior) + "\n") if prior else "").splitlines(True),
                        (canonical(cfg) + "\n").splitlines(True),
                        fromfile=".nexkit/project.json",
                        tofile=".nexkit/project.json",
                    )
                ),
            }
        )
    if cfg["schema"] == 2:
        # Files explicitly returned to consumer ownership stay on disk and are
        # dropped from the ledger, so uninstall cannot later remove them.
        for name in previous_bundle - {
            name for name, record in cfg["files"].items() if record["managed"]
        }:
            if name in cfg["files"]:
                changes.append({"path": name, "operation": "preserve-as-consumer", "diff": ""})
            ledger["files"].pop(name, None)
    return files, ledger, changes


def install(root, cfg, hosts, *, apply=False, bundle=None):
    root = Path(root).resolve()
    files, ledger, changes = installation_plan(root, cfg, hosts, bundle=bundle)
    if apply:
        # All conflicts are detected before writing any managed file.
        for name, content in files.items():
            path = root / name
            if content is None:
                path.unlink()
                ledger["files"].pop(name, None)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            ledger["files"][name] = file_hash(path)
        if cfg["schema"] == 2:
            ledger["bundle_files"] = [
                name for name, record in cfg["files"].items() if record["managed"]
            ]
            ledger["project"] = digest(cfg)
            write_json(root / ".nexkit/project.json", cfg)
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
    project_path = consumer_path(root, ".nexkit/project.json")
    if project_path.exists():
        cfg = read_json(project_path)
        if cfg.get("schema") == 2 and digest(cfg) == ledger.get("project"):
            for name in ledger.get("bundle_files", []):
                bundle_path(name)
                if cfg["files"].get(name, {}).get("managed") is True:
                    owned.add(name)
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
    if cfg["schema"] == 2:
        results = {
            name: doctor(root, effective_config(cfg, name), online=online, checks=checks)
            for name in cfg["pipelines"]
        }
        return {
            "ready": all(result["ready"] for result in results.values()),
            "ready_scope": "each configured pipeline; native workflow execution needs Actions evidence",
            "pipelines": results,
            "live_agent_verified": False,
        }
    problems, verified = [], []
    binding = cfg.get("binding", {})
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
    for name, record in binding.get("files", {}).items():
        path = consumer_path(root, name)
        if not path.is_file() or file_hash(path) != record["sha256"]:
            problems.append(f"Accepted workflow/control file missing or changed: {name}")
    verification = None
    if checks and cfg.get("checks"):
        verification = verify(cfg, root)
        if not verification["passed"]:
            problems.append("Consumer verification failed or is not yet possible")
            if cfg.get("application") == "absent":
                problems.append("Initial repository has no verified application behavior")
        else:
            verified.append("consumer checks and E2E executed")
    elif checks:
        problems.append(
            "This pipeline's native workflow execution has not been verified in Actions"
        )
    else:
        problems.append("Consumer checks were not executed (use --checks)")
    if online:
        gh = GitHub(cfg["repository"])
        try:
            repository = gh.repo()
            require(repository["default_branch"] == cfg["default_branch"], "Default branch drift")
            if not binding or "delivery" in binding["entrypoints"]:
                gh.audit_settings(cfg["default_branch"])
                policy = gh.api(f"{gh.root}/actions/permissions/workflow")
                require(
                    policy.get("can_approve_pull_request_reviews") is True,
                    "Actions permission to create pull requests is disabled",
                )
            if cfg.get("engine") and authentication(cfg) == "api-key":
                secrets = gh.api(f"{gh.root}/actions/secrets")["secrets"]
                require(
                    any(x["name"] == "OPENAI_API_KEY" for x in secrets),
                    "OPENAI_API_KEY Actions secret is missing (an org secret may require admin verification)",
                )
                verified.append("API secret metadata inspected")
            if cfg.get("environment") and isinstance(agent_runner(cfg), list):
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
        "engine": cfg.get("engine"),
        "models": cfg.get("models", {}),
        "reasoning_effort": cfg.get("reasoning_effort", {}),
        "authentication": authentication(cfg) if cfg.get("engine") else None,
        "agent_runner": agent_runner(cfg) if cfg.get("environment") else None,
        "limits": cfg.get("limits", {}),
        "clarification": clarification_limits(cfg)
        if "clarification" in cfg or not binding or "clarify" in binding["entrypoints"]
        else None,
        "live_agent_verified": False,
    }
