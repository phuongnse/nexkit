"""Survey and install from actual user decisions; there are no project presets."""

from __future__ import annotations

import difflib
import hashlib
import re
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

HOSTS = {"codex": ".agents/skills"}


def literal_concurrency_group(text):
    """Inspect ordinary literal groups; native Actions validates full YAML semantics."""
    match = re.search(r"(?m)^concurrency:[ \t]*([^\n]*)", text)
    if not match:
        return None
    value = match[1].strip()
    literal = r"['\"]?([A-Za-z0-9_-]+)['\"]?"
    if not value or value.startswith("#"):
        block = re.split(r"(?m)^\S", text[match.end() :], 1)[0]
        group = re.search(r"(?m)^[ \t]+group:[ \t]*" + literal + r"[ \t]*(?:#.*)?$", block)
    elif value.startswith("{"):
        group = re.search(r"(?:\{|,)[ \t]*group:[ \t]*" + literal + r"[ \t]*[,}]", value)
    else:
        group = re.fullmatch(literal + r"[ \t]*(?:#.*)?", value)
    return group[1] if group else None


def workflow_concurrency_problem(name, content, cfg, *, kit=None):
    """Catch known adapter deadlocks without adding a YAML runtime or executor."""
    if not name.startswith(".github/workflows/"):
        return None
    text = content.decode()
    group = literal_concurrency_group(text)
    if not group:
        return None
    for adapter in ("delivery", "clarify", "intake", "release"):
        reference = re.escape(cfg["kit"]["repository"] + f"/.github/workflows/{adapter}.yml@")
        if re.search(
            r"(?m)^    uses:[ \t]*['\"]?" + reference + r"[0-9a-f]{40}['\"]?[ \t]*(?:#.*)?$", text
        ):
            child = ((kit or kit_root()) / ".github/workflows" / f"{adapter}.yml").read_text(
                encoding="utf-8"
            )
            if group == literal_concurrency_group(child):
                return (
                    f"Workflow {name} repeats the {adapter} adapter's concurrency group '{group}'. "
                    "Remove caller concurrency for a standalone adapter, or use the granular "
                    "composition workflows under one caller concurrency group."
                )
    return None


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
            context[p] = path.read_text(encoding="utf-8", errors="replace")[:10000]
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


def managed_files(cfg, hosts, *, root=None, bundle=None, kit=None, consumer_updates=None):
    kit = Path(kit or kit_root())
    consumer_updates = consumer_updates or {}
    payload = {}
    for host in hosts:
        require(host in HOSTS, f"Unknown host {host}")
        for path in sorted((kit / "plugins/nexkit/skills").rglob("*")):
            if path.is_file():
                rel = path.relative_to(kit / "plugins/nexkit/skills")
                payload[f"{HOSTS[host]}/{rel.as_posix()}"] = path.read_bytes()
    for name, record in cfg["files"].items():
        # Accepted consumer files are inspected, never adopted implicitly.
        source = consumer_path(bundle if record["managed"] and bundle else root, name)
        require(source.is_file(), f"Accepted setup file is missing: {name}")
        content = consumer_updates.get(name, source.read_bytes())
        require(
            hashlib.sha256(content).hexdigest() == record["sha256"],
            f"Setup file hash mismatch: {name}",
        )
        problem = workflow_concurrency_problem(name, content, cfg, kit=kit)
        require(problem is None, problem)
        if record["managed"] or name in consumer_updates:
            payload[name] = content
    return payload


def installation_plan(root, cfg, hosts, *, bundle=None, kit=None, consumer_updates=None):
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
    consumer_updates = consumer_updates or {}
    for name in consumer_updates:
        require(
            name.startswith(".github/workflows/")
            and (prior or {}).get("files", {}).get(name, {}).get("managed") is False
            and cfg["files"].get(name, {}).get("managed") is False
            and file_hash(consumer_path(root, name)) == prior["files"][name]["sha256"],
            f"Consumer edits preserved: {name}. Reconcile the workflow before selecting a release",
        )
    files = managed_files(
        cfg, hosts, root=root, bundle=bundle, kit=kit, consumer_updates=consumer_updates
    )
    owned = {
        name
        for name, record in (prior or {}).get("files", {}).items()
        if record.get("managed") is True and name in ledger.get("bundle_files", [])
    }
    previous_bundle = set(ledger.get("bundle_files", []))
    if kit is not None:
        # A selected release may remove skills. Keep ownership across downgrade
        # without leaving the newer kit's instructions active in the project.
        for name, expected in ledger["files"].items():
            if name not in files and any(
                name.startswith(prefix + "/") for prefix in HOSTS.values()
            ):
                path = consumer_path(root, name)
                if path.exists():
                    require(file_hash(path) == expected, f"Consumer edits preserved: {name}")
                    files[name] = None
    # The desired manifest is the full workflow set. Retire only previously
    # accepted managed files. Unrelated consumer files stay on disk.
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
                name in consumer_updates
                or (name in ledger["files"] and file_hash(path) == ledger["files"][name]),
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
        elif name in cfg["files"] and name not in ledger["files"]:
            changes.append({"path": name, "operation": "adopt", "diff": ""})
    if prior != cfg:
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
    # Files explicitly returned to consumer ownership stay on disk and are
    # dropped from the ledger, so uninstall cannot later remove them.
    for name in previous_bundle - {
        name for name, record in cfg["files"].items() if record["managed"]
    }:
        if name in cfg["files"]:
            changes.append({"path": name, "operation": "preserve-as-consumer", "diff": ""})
        ledger["files"].pop(name, None)
    return files, ledger, changes


def install(root, cfg, hosts, *, apply=False, bundle=None, kit=None, consumer_updates=None):
    root = Path(root).resolve()
    files, ledger, changes = installation_plan(
        root, cfg, hosts, bundle=bundle, kit=kit, consumer_updates=consumer_updates
    )
    version = cfg["kit"]["version"] if kit is not None else __version__
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
            if name not in (consumer_updates or {}):
                ledger["files"][name] = file_hash(path)
        ledger["bundle_files"] = [
            name for name, record in cfg["files"].items() if record["managed"]
        ]
        ledger["project"] = digest(cfg)
        write_json(root / ".nexkit/project.json", cfg)
        ledger.update(version=version, hosts=sorted(set(ledger.get("hosts", []) + hosts)))
        write_json(root / ".nexkit/installation.json", ledger)
    return {
        "applied": apply,
        "version": version,
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
    project_path = consumer_path(root, ".nexkit/project.json")
    if project_path.exists():
        cfg = read_json(project_path)
        if cfg.get("schema") == 1 and digest(cfg) == ledger.get("project"):
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
    config(cfg)
    results = {
        name: doctor_pipeline(root, effective_config(cfg, name), online=online, checks=checks)
        for name in cfg["pipelines"]
    }
    return {
        "ready": all(result["ready"] for result in results.values()),
        "ready_scope": "each configured pipeline; native workflow execution needs Actions evidence",
        "pipelines": results,
        "live_agent_verified": False,
    }


def doctor_pipeline(root, cfg, *, online=False, checks=False):
    from .checks import verify
    from .github import GitHub

    problems, verified, warnings = [], [], []
    binding = cfg.get("binding", {})
    invocations = {}
    if "invocations" in binding:
        from .invocations import execution_config

        for name, definition in binding["invocations"].items():
            execution = execution_config({"config": cfg, "invocation": {"definition": definition}})
            invocations[name] = {
                **definition,
                "agent_runner": agent_runner(execution),
                "setup": execution["environment"]["setup"],
            }
    if cfg["kit"]["version"] != __version__:
        warnings.append(
            "Local toolkit version differs from the project's CI pin; "
            "CI uses the accepted kit.ref, while this command validates its supported schema"
        )
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
        elif problem := workflow_concurrency_problem(name, path.read_bytes(), cfg):
            problems.append(problem)
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
            if "delivery" in binding["entrypoints"]:
                gh.audit_settings(cfg["default_branch"], cfg)
                policy = gh.api(f"{gh.root}/actions/permissions/workflow")
                require(
                    policy.get("can_approve_pull_request_reviews") is True,
                    "Actions permission to create pull requests is disabled",
                )
            from .adapters import adapter

            secret = (
                adapter(cfg["engine"]).required_secret(cfg["engine"]) if cfg.get("engine") else None
            )
            if secret:
                secrets = gh.api(f"{gh.root}/actions/secrets")["secrets"]
                require(
                    any(x["name"] == secret for x in secrets),
                    f"{secret} Actions secret is missing (an org secret may require admin verification)",
                )
                verified.append("API secret metadata inspected")
            runner_labels = [value["agent_runner"] for value in invocations.values()]
            if cfg.get("environment") and (not invocations or "clarify" in binding["entrypoints"]):
                runner_labels.append(agent_runner(cfg))
            runner_labels = [labels for labels in runner_labels if isinstance(labels, list)]
            if runner_labels:
                runners = gh.api(
                    f"{gh.root}/actions/runners?per_page=100", pages=True, collection="runners"
                )
                require(
                    all(
                        any(
                            r.get("status") == "online"
                            and set(labels)
                            <= {label["name"].lower() for label in r.get("labels", [])}
                            for r in runners
                        )
                        for labels in runner_labels
                    ),
                    "An invocation has no online runner matching its agent_runner labels",
                )
                verified.append(
                    "project runner registration inspected; login and isolation require a live probe"
                )
            verified.append("GitHub settings inspected")
        except Blocked as exc:
            problems.append(str(exc))
    else:
        problems.append("GitHub permissions/settings/authentication have not been verified")
    from .completion import settings as completion_settings

    return {
        "ready": not problems,
        "ready_scope": "configuration and declared checks; live model access is reported separately",
        "verified": verified,
        "warnings": warnings,
        "kit": {"local_version": __version__, "configured": cfg["kit"]},
        "problems": problems,
        "verification": verification,
        "engine": cfg.get("engine"),
        "models": cfg.get("models", {}),
        "reasoning_effort": cfg.get("reasoning_effort", {}),
        "invocations": invocations,
        "steps": binding.get("steps", {}),
        "approvals": binding.get("approvals", {}),
        "issue_completion": completion_settings(cfg),
        "authentication": authentication(cfg) if cfg.get("engine") else None,
        "agent_runner": agent_runner(cfg) if cfg.get("environment") else None,
        "limits": cfg.get("limits", {}),
        "clarification": clarification_limits(cfg)
        if "clarification" in cfg or "clarify" in binding["entrypoints"]
        else None,
        "live_agent_verified": False,
    }
