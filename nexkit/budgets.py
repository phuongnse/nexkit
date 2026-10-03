"""Exact, issue-scoped administrator grants. Consumption is never reset."""

from __future__ import annotations

import json
import re

from .common import canonical, digest
from .policy import human, now, require, spec_hash

FIELDS = {"agent_calls": 40, "attempts": 20, "minutes": 1440, "clarification_calls": 1000}
COMMAND = re.compile(r"/nexkit budget ([0-9a-f]{64}) (\{[^\n]+\})")


def additions(value):
    require(
        isinstance(value, dict) and value and set(value) <= FIELDS.keys(),
        "Invalid budget additions",
    )
    require(
        all(type(number) is int and 0 < number <= FIELDS[name] for name, number in value.items()),
        "Budget additions must be bounded positive integers",
    )
    return value


def binding(issue, cfg):
    return {
        "repository": cfg["repository"],
        "issue": issue["number"],
        "spec": spec_hash(issue),
        "edited_at": issue.get("last_edited_at"),
        "config": digest(cfg),
        "pipeline": cfg["binding"]["pipeline"],
        "kit": cfg["kit"]["ref"],
    }


def subject(issue, cfg, state):
    return {
        **binding(issue, cfg),
        "checkpoint": digest(state.get("recovery", {})),
        "candidate": state.get("candidate"),
        "run_key": state.get("run_key"),
        "clarification_run": state.get("clarification", {}).get("run_key"),
        "used": {key: state.get(key, 0) for key in ("attempts", "agent_calls", "delivery_calls")},
        "clarification_calls": state.get("clarification", {}).get("calls", 0),
        "grants": digest(state.get("budget_grants", [])),
        "budget_wait": state.get("budget_wait"),
    }


def proposal(issue, cfg, state, extra):
    extra = additions(extra)
    require(
        state.get("status") not in {"merged", "released", "completed"},
        "Completed work cannot receive execution budget",
    )
    target = subject(issue, cfg, state)
    identifier = digest({"subject": target, "additions": extra})
    return {
        "subject": target,
        "additions": extra,
        "human_comment": f"/nexkit budget {identifier} {canonical(extra)}",
        "note": "An actual repository administrator posts this exact command after reviewing the current work. It does not approve a merge or extend a call timeout.",
    }


def receipt_valid(gh, issue, cfg, record, comments):
    require(
        isinstance(record, dict)
        and {
            "binding",
            "comment",
            "command",
            "created_at",
            "actor",
            "subject",
            "additions",
            "applied_at",
        }
        <= set(record)
        and isinstance(record["binding"], dict)
        and isinstance(record["subject"], dict)
        and isinstance(record["command"], str)
        and type(record["comment"]) is int
        and record["comment"] > 0
        and isinstance(record["created_at"], str),
        "Invalid budget receipt",
    )
    extra = additions(record["additions"])
    require(
        record["binding"] == binding(issue, cfg),
        "Budget grant belongs to changed specification/configuration",
    )
    matches = [comment for comment in comments if comment.get("id") == record["comment"]]
    require(len(matches) == 1, "Budget approval was removed")
    comment = matches[0]
    match = COMMAND.fullmatch(record["command"])
    try:
        amounts = json.loads(match[2]) if match else None
    except ValueError:
        amounts = None
    require(
        match is not None
        and amounts == extra
        and match[1] == digest({"subject": record["subject"], "additions": record["additions"]})
        and all(record["subject"].get(key) == value for key, value in record["binding"].items()),
        "Budget receipt does not match its exact approved amounts",
    )
    require(
        comment.get("body", "").strip() == record["command"]
        and comment.get("created_at") == comment.get("updated_at")
        and comment.get("created_at") == record["created_at"]
        and comment.get("user", {}).get("login") == record["actor"]
        and human(comment, gh.permission)
        and gh.permission(record["actor"]) == "admin",
        "Budget approval was changed or its administrator is no longer authorized",
    )


def validate(gh, issue, cfg, state):
    require(isinstance(state.get("budget_grants", []), list), "Invalid budget grant inventory")
    if state.get("budget_grants"):
        comments = gh.comments(issue["number"])
        for record in state["budget_grants"]:
            receipt_valid(gh, issue, cfg, record, comments)


def reconcile(gh, issue, cfg, state):
    """Apply a real approval once, immediately before the next reservation."""
    from .checkpoints import reconcile_discard

    discarded = reconcile_discard(gh, issue, cfg, state)
    active = []
    for record in state.get("budget_grants", []):
        require(
            isinstance(record, dict) and isinstance(record.get("binding"), dict),
            "Invalid budget receipt",
        )
        if record["binding"] == binding(issue, cfg):
            active.append(record)
        else:
            state.setdefault("budget_grant_history", []).append(
                {
                    **record,
                    "expired_at": now(),
                    "reason": "Accepted specification/configuration changed",
                }
            )
    if "budget_grants" in state:
        state["budget_grants"] = active
    validate(gh, issue, cfg, state)
    comments = gh.comments(issue["number"])
    used = {record["comment"] for record in state.get("budget_grants", [])}
    applied = False
    for comment in comments:
        match = COMMAND.fullmatch(comment.get("body", "").strip())
        if (
            not match
            or comment.get("id") in used
            or not human(comment, gh.permission)
            or gh.permission(comment["user"]["login"]) != "admin"
            or comment.get("created_at") != comment.get("updated_at")
        ):
            continue
        try:
            extra = additions(json.loads(match[2]))
        except (ValueError, TypeError, RuntimeError):
            continue
        target = subject(issue, cfg, state)
        if match[1] != digest({"subject": target, "additions": extra}):
            continue  # Stale decisions cannot grant capacity to a new subject.
        require(
            len(canonical(state).encode()) < 600000,
            "Budget decision history exceeds the issue state bound; preserve it and prepare a separate work item",
        )
        state.setdefault("budget_grants", []).append(
            {
                "binding": binding(issue, cfg),
                "subject": target,
                "additions": extra,
                "comment": comment["id"],
                "actor": comment["user"]["login"],
                "command": comment["body"].strip(),
                "created_at": comment["created_at"],
                "applied_at": now(),
            }
        )
        applied = True
    return applied or discarded


def end_wait(state, *, clock=None):
    if state.get("budget_wait"):
        from datetime import datetime

        interval = (
            datetime.fromisoformat(clock or now())
            - datetime.fromisoformat(state["budget_wait"]["opened_at"])
        ).total_seconds()
        require(interval >= 0, "Invalid budget wait interval")
        if state.get("started_at"):
            state["budget_wait_seconds"] = state.get("budget_wait_seconds", 0) + interval
        state.pop("budget_wait")


def mark_wait(state, reason):
    if "exhausted" in reason.lower() and any(
        word in reason.lower() for word in ("budget", "attempts")
    ):
        state.setdefault("budget_wait", {"opened_at": now(), "reason": reason})


def limit(state, cfg, name, default=None):
    base = cfg.get("limits", {}).get(name, default)
    if name == "clarification_calls":
        base = cfg.get("clarification", {}).get("max_calls", default)
    if base is None:
        return None
    return base + sum(record["additions"].get(name, 0) for record in state.get("budget_grants", []))


def status(issue, cfg, state):
    from .policy import delivery_budget_used, delivery_elapsed

    limits = {name: limit(state, cfg, name) for name in FIELDS}
    used = {
        "agent_calls": delivery_budget_used(state, cfg),
        "attempts": state.get("attempts", 0),
        "clarification_calls": state.get("clarification", {}).get("calls", 0),
        "minutes": delivery_elapsed(state) / 60 if state.get("started_at") else 0,
    }
    return {
        "limits": limits,
        "used": used,
        "remaining": {
            name: max(0, cap - used[name]) if cap is not None else None
            for name, cap in limits.items()
        },
        "grant_count": len(state.get("budget_grants", [])),
        "subject": digest(subject(issue, cfg, state)),
    }
