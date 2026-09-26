"""Deterministic decisions. LLM output is data and can never grant authority."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from .common import Blocked, digest, safe_path

SHA = re.compile(r"[0-9a-f]{40}\Z")
HASH = re.compile(r"[0-9a-f]{64}\Z")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?\Z")
HUMAN_PERMISSIONS = {"write", "maintain", "admin"}
STATE_BRANCH = "nexkit/state"
PROTECTED = (".nexkit/project.json", ".nexkit/installation.json")
HOST_DIRS = (".codex", ".agents", ".claude", ".gemini", ".cursor", ".agent")


def authentication(cfg):
    return cfg["engine"].get("auth", "api-key")


def agent_runner(cfg):
    return cfg["environment"].get("agent_runner", cfg["environment"]["runner"])


def clarification_limits(cfg):
    if "clarification" in cfg:
        return {
            "max_calls": cfg["clarification"].get("max_calls"),
            "agent_minutes": cfg["clarification"]["agent_minutes"],
            "shared_delivery_budget": False,
        }
    limits = cfg["limits"]
    return {
        "max_calls": limits["attempts"],
        "agent_minutes": max(1, min(15, limits["minutes"] // limits["attempts"])),
        "shared_delivery_budget": True,
    }


def delivery_calls(state):
    # Older state only recorded total calls and the clarification subtotal.
    count = state.get(
        "delivery_calls",
        state.get("agent_calls", 0) - state.get("clarification", {}).get("calls", 0),
    )
    require(type(count) is int and count >= 0, "Invalid delivery invocation accounting")
    return count


def delivery_budget_used(state, cfg):
    return delivery_calls(state) if "clarification" in cfg else state.get("agent_calls", 0)


def require(condition, message):
    if not condition:
        raise Blocked(message)


def now():
    return datetime.now(timezone.utc).isoformat()


def spec_hash(issue):
    require(
        isinstance(issue.get("title"), str) and isinstance(issue.get("body"), str),
        "Issue must contain a title and a requirement body",
    )
    return digest({"title": issue["title"], "body": issue["body"]})


def human(comment, permission):
    user = comment.get("user", {})
    return (
        user.get("type") == "User"
        and not user.get("login", "").endswith("[bot]")
        and permission(user.get("login", "")) in HUMAN_PERMISSIONS
    )


def approval(issue, comments, permission, *, release=False):
    require(issue.get("state") == "open", "Work item is closed")
    expected = spec_hash(issue)
    verb = "release" if release else "approve"
    command = f"/nexkit {verb} {expected}"
    matches = [
        c
        for c in comments
        if c.get("body", "").strip() == command
        and c.get("created_at") == c.get("updated_at")
        and (not issue.get("last_edited_at") or c.get("created_at", "") > issue["last_edited_at"])
        and human(c, permission)
    ]
    require(matches, f"Awaiting an authorized human comment: {command}")
    chosen = max(matches, key=lambda c: c["id"])
    # The exact current comment is re-fetched at every consequential boundary.
    return {
        "comment_id": chosen["id"],
        "actor": chosen["user"]["login"],
        "spec": expected,
        "comment_updated_at": chosen.get("updated_at"),
        "requirement_edited_at": issue.get("last_edited_at"),
    }


def control_command(comments, permission):
    commands = [
        c
        for c in comments
        if c.get("body", "").strip() in {"/nexkit cancel", "/nexkit resume"}
        and human(c, permission)
    ]
    if not commands:
        return None
    return max(commands, key=lambda c: c["id"])["body"].strip().split()[-1]


def positive(value, label, maximum):
    require(
        type(value) is int and 0 < value <= maximum, f"{label} must be an integer in 1..{maximum}"
    )


def command(value):
    require(
        isinstance(value, list)
        and value
        and all(isinstance(x, str) and x and "\x00" not in x for x in value),
        "Commands must be nonempty argument arrays; shell strings are not accepted",
    )


def config(value):
    if isinstance(value, dict) and value.get("schema") == 2:
        from .pipelines import validate_project

        return validate_project(value)
    require(isinstance(value, dict) and value.get("schema") == 1, "Expected project schema 1")
    require(REPO.fullmatch(value.get("repository", "")), "Set repository as owner/name")
    branch = value.get("default_branch", "")
    require(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/_.-]*", branch) and ".." not in branch,
        "Invalid default branch",
    )
    kit = value.get("kit", {})
    require(
        REPO.fullmatch(kit.get("repository", "")) and SHA.fullmatch(kit.get("ref", "")),
        "Pin kit.repository and kit.ref to a full commit SHA",
    )
    require(VERSION.fullmatch(kit.get("version", "")), "Set the exact kit version")
    scoped = "binding" in value
    hooks = set(value["binding"]["entrypoints"]) if scoped else {"clarify", "delivery", "release"}
    needs_agent = bool(hooks & {"clarify", "delivery"})
    engine = value.get("engine", {})
    require(isinstance(engine, dict), "Declare engine as an object")
    if needs_agent or "engine" in value:
        require(engine.get("name") == "codex", "This version provides the Codex CI engine")
        require(VERSION.fullmatch(engine.get("version", "")), "Pin the Codex CLI version")
        require(
            authentication(value) in ("api-key", "chatgpt"),
            "Choose engine.auth: api-key or chatgpt",
        )
    roles = (
        {"implement", "review"} if "delivery" in hooks else {"implement"} if needs_agent else set()
    )
    models = value.get("models", {})
    require(isinstance(models, dict), "Declare models as a mapping")
    for role in roles | set(models):
        model = value.get("models", {}).get(role)
        require(
            isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9._:/-]{1,100}", model),
            f"Choose an accessible model for {role}; no implicit model default",
        )
    if "reasoning_effort" in value:
        efforts = value["reasoning_effort"]
        require(
            isinstance(efforts, dict) and roles <= set(efforts) <= set(models),
            "reasoning_effort must match configured model roles",
        )
        for role, effort in efforts.items():
            require(
                effort in ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"),
                f"Choose a supported reasoning_effort for {role}",
            )
    limits = value.get("limits", {})
    require(isinstance(limits, dict), "Declare limits as an object")
    if scoped:
        require(
            set(limits) <= {"attempts", "agent_calls", "minutes", "command_seconds"},
            "Unknown execution limit",
        )
    needs_limits = (
        not scoped
        or bool(hooks & {"delivery", "release"})
        or ("clarify" in hooks and "clarification" not in value)
    )
    for key, maximum in (
        ("attempts", 20),
        ("agent_calls", 40),
        ("minutes", 1440),
        ("command_seconds", 3600),
    ):
        required = needs_limits and (key != "agent_calls" or not scoped or needs_agent)
        if required or key in limits:
            positive(limits.get(key), f"limits.{key}", maximum)
    if "clarification" in value:
        clarification = value["clarification"]
        require(
            isinstance(clarification, dict)
            and set(clarification) <= {"agent_minutes", "max_calls"},
            "Clarification accepts agent_minutes and optional max_calls",
        )
        positive(clarification.get("agent_minutes"), "clarification.agent_minutes", 60)
        maximum = clarification.get("max_calls")
        require(
            maximum is None or (type(maximum) is int and maximum > 0),
            "clarification.max_calls must be a positive integer or null for no conversation cap",
        )
    if not scoped or "delivery" in hooks:
        require(
            limits["agent_calls"] >= (2 if "clarification" in value else 3),
            "Reserve at least implementation and independent review, plus clarification when shared",
        )
    environment = value.get("environment", {})
    require(isinstance(environment, dict), "Declare environment as an object")
    if needs_agent or needs_limits or environment:
        require(
            environment.get("runner") == "ubuntu-24.04",
            "Controller, verification and release jobs require GitHub-hosted ubuntu-24.04",
        )
    runner = agent_runner(value) if environment else None
    if isinstance(runner, list):
        require(
            4 <= len(runner) <= 8
            and all(
                isinstance(label, str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", label)
                for label in runner
            ),
            "Self-hosted agent_runner must contain Linux/x64 labels and a project-specific label",
        )
        require(
            len(set(runner)) == len(runner) and {"self-hosted", "linux", "x64"} < set(runner),
            "Use self-hosted, linux, x64 and at least one project-specific runner label",
        )
    elif environment:
        require(runner == "ubuntu-24.04", "Unsupported agent runner")
    if engine and authentication(value) == "chatgpt":
        require(
            isinstance(runner, list),
            "ChatGPT CI authentication requires an explicitly configured self-hosted agent runner",
        )
        require(
            engine["version"] == "0.156.1",
            "ChatGPT isolation is integrated with Codex 0.156.1; verify other versions before upgrading",
        )
    if environment:
        require(isinstance(environment.get("setup"), list), "Declare environment.setup commands")
        if environment["setup"]:
            positive(limits.get("command_seconds"), "limits.command_seconds for setup", 3600)
    for item in environment.get("setup", []):
        command(item)
    require(
        isinstance(value.get("decisions"), list) and value["decisions"],
        "Record the accepted project decisions",
    )
    require(all(isinstance(s, str) and s for s in value["decisions"]), "Invalid decisions")
    require(isinstance(value.get("knowledge"), list), "Declare knowledge source paths")
    for path in value["knowledge"]:
        safe_path(path)
    checks = value.get("checks", [] if scoped and not hooks & {"delivery", "release"} else None)
    require(isinstance(checks, list), "Declare checks, including explicit test and E2E commands")
    if checks:
        require(environment, "Checks need an explicit environment")
        positive(limits.get("command_seconds"), "limits.command_seconds for checks", 3600)
    names = set()
    kinds = set()
    for check in checks:
        require(isinstance(check, dict), "Declare each check as an object")
        name = check.get("name", "")
        require(
            re.fullmatch(r"[a-z][a-z0-9-]{0,47}", name) and name not in names,
            "Check names must be unique lowercase identifiers",
        )
        names.add(name)
        kind = check.get("kind")
        require(kind in ("build", "lint", "test", "e2e"), "Invalid check kind")
        kinds.add(kind)
        command(check.get("argv"))
        positive(
            check.get("timeout_seconds"),
            "check.timeout_seconds",
            limits.get("command_seconds", 3600),
        )
        if kind in ("test", "e2e"):
            report = check.get("report", {})
            require(
                report.get("format") in ("junit", "tap", "unittest"),
                "Test/E2E checks need a real JUnit, TAP or unittest result",
            )
            if report["format"] == "junit":
                safe_path(report.get("path"))
    # A new repository may be configured before the app exists, but cannot be ready.
    if not scoped or hooks & {"delivery", "release"} or "application" in value:
        require(value.get("application") in ("present", "absent"), "Declare whether an app exists")
    if value.get("application") == "present" and (not scoped or hooks & {"delivery", "release"}):
        require({"test", "e2e"} <= kinds, "Application requires test and E2E commands")
    release = value.get("release", {})
    require(isinstance(release, dict), "Declare release as an object")
    if not scoped or "release" in hooks or release:
        require(type(release.get("enabled")) is bool, "Explicitly configure release.enabled")
    if release.get("enabled"):
        command(release.get("build"))
        require(
            isinstance(release.get("artifacts"), list) and release["artifacts"],
            "Declare the exact release artifact paths",
        )
        for path in release["artifacts"]:
            safe_path(path)
        require(
            re.fullmatch(r"[A-Za-z0-9._-]{0,30}", release.get("tag_prefix", "")) is not None,
            "Invalid tag prefix",
        )
    if not scoped or "delivery" in hooks or "merge_method" in value:
        require(value.get("merge_method") in ("squash", "merge", "rebase"), "Choose merge_method")
    return value


def reserve(state, cfg, run_key, *, clock=None):
    """Reserve costs BEFORE any CLI runs. Failed/retried runs consume the reservation."""
    stamp = clock or now()
    state = dict(state)
    started = state.setdefault("started_at", stamp)
    elapsed = (datetime.fromisoformat(stamp) - datetime.fromisoformat(started)).total_seconds()
    require(elapsed < cfg["limits"]["minutes"] * 60, "Total delivery time budget exhausted")
    reservations = state.setdefault("reservations", [])
    require(run_key not in reservations, "This run attempt has already been reserved")
    require(state.get("attempts", 0) < cfg["limits"]["attempts"], "Delivery attempts exhausted")
    require(
        delivery_budget_used(state, cfg) + 2 <= cfg["limits"]["agent_calls"],
        "Agent invocation budget exhausted",
    )
    state["reservations"] = [*reservations, run_key]
    state["attempts"] = state.get("attempts", 0) + 1
    state["delivery_calls"] = delivery_calls(state) + 2
    state["agent_calls"] = state.get("agent_calls", 0) + 2
    return state


def candidate_key(issue, cfg, base, head):
    require(SHA.fullmatch(base) and SHA.fullmatch(head), "Candidate commits must be full SHAs")
    return {
        "spec": spec_hash(issue),
        "config": digest(cfg),
        "base": base,
        "head": head,
        "kit": cfg["kit"]["ref"],
    }


def protected_path(path, cfg=None):
    safe_path(path)
    return (
        path in PROTECTED
        or path in (cfg or {}).get("binding", {}).get("files", {})
        or (
            "binding" in (cfg or {})
            and path.startswith((".github/workflows/", ".github/actions/", ".nexkit/controls/"))
        )
        or path.startswith(".github/workflows/nexkit")
        or path.startswith(".github/skills/nexkit-")
        or any(path == p or path.startswith(p + "/") for p in HOST_DIRS)
    )


def agent_result(value, role):
    require(isinstance(value, dict), "Agent output must be a JSON object")
    require(value.get("status") in ("done", "blocked"), "Missing/invalid agent status")
    require(
        isinstance(value.get("summary"), str) and value["summary"].strip(), "Missing agent summary"
    )
    require(
        isinstance(value.get("skills_used"), list) and f"nexkit-{role}" in value["skills_used"],
        f"Agent must report using the installed nexkit-{role} skill",
    )
    for field in ("skills_used", "commands", "limitations"):
        require(
            isinstance(value.get(field), list) and all(isinstance(x, str) for x in value[field]),
            f"Invalid agent {field}",
        )
    require(value["commands"], "Agent must report the tools/commands actually used")
    if role == "review":
        require(
            value.get("verdict") in ("approve", "changes_requested", "blocked"),
            "Missing/invalid independent review verdict",
        )
        findings = value.get("findings")
        require(
            isinstance(findings, list)
            and all(
                isinstance(f, dict)
                and isinstance(f.get("detail"), str)
                and f["detail"].strip()
                and f.get("severity") in ("blocking", "suggestion")
                for f in findings
            ),
            "Invalid reviewer findings",
        )
        require(
            not (
                value["verdict"] == "approve" and any(f["severity"] == "blocking" for f in findings)
            ),
            "Reviewer cannot approve unresolved blocking findings",
        )
        require(
            isinstance(value.get("acceptance"), list)
            and value["acceptance"]
            and all(
                isinstance(x, dict)
                and isinstance(x.get("criterion"), str)
                and isinstance(x.get("evidence"), str)
                and x["evidence"].strip()
                and type(x.get("passed")) is bool
                for x in value["acceptance"]
            ),
            "Review must trace requirement criteria to actual evidence",
        )
        if value["verdict"] == "approve":
            require(all(x["passed"] for x in value["acceptance"]), "Acceptance criteria are unmet")
    return value


def merge_gate(key, verification, review, cfg=None):
    for name, value in (("verification", verification), ("review", review)):
        require(value.get("candidate") == key, f"Stale or mismatched {name}")
    require(verification.get("passed") is True, "Verification failed or was skipped")
    require(verification.get("checks"), "No checks actually ran")
    if cfg is not None:
        from .checks import complete_checks

        require(
            key["config"] == digest(cfg), "Candidate configuration differs from the merge policy"
        )
        complete_checks(cfg, verification["checks"])
    require(
        {"test", "e2e"} <= {c.get("kind") for c in verification["checks"]},
        "Test and end-to-end checks are both required",
    )
    require(all(c.get("passed") is True for c in verification["checks"]), "A required check failed")
    require(
        all(
            type(c.get("tests")) is int and c["tests"] > 0
            for c in verification["checks"]
            if c.get("kind") in ("test", "e2e")
        ),
        "Test evidence contains no executed cases",
    )
    result = agent_result(review.get("result"), "review")
    require(
        result["status"] == "done" and result["verdict"] == "approve", "Reviewer did not approve"
    )
    require(review.get("unchanged") is True, "Reviewer modified the candidate")
    require(review.get("independent") is True, "Independent reviewer session is missing")
