"""Restore the caches in `cache` before setup, for the agent and verify jobs.

Those jobs only restore. They run code the agent wrote, and NexKit's runs use the base
branch's cache scope, so a cache they saved would reach every later run there. The
repository's own workflow on the base branch saves the caches under the same keys.
"""

from __future__ import annotations

DOCS = "https://github.com/phuongnse/nexkit/blob/main/docs/configuration.md#caches"


def key_prefix(runner_os, name):
    """The key without the hash of the key files; it also finds a partial match."""
    return f"nexkit-{runner_os}-{name}-"


def _in_checkout(pattern, repo):
    negate = "!" if pattern.startswith("!") else ""
    return f"{negate}{repo.rstrip('/')}/{pattern.removeprefix('!').removeprefix('./')}"


def plan(cfg, repo, runner_os):
    """Step outputs for the restore action. Entry N is in `nameN`, `pathsN`, `prefixN` and
    `filesN`: the key files as patterns from the workspace, for `hashFiles`."""
    outputs = {"count": str(len(cfg["cache"]))}
    for number, entry in enumerate(cfg["cache"], 1):
        outputs[f"name{number}"] = entry["name"]
        outputs[f"paths{number}"] = "\n".join(entry["paths"])
        outputs[f"prefix{number}"] = key_prefix(runner_os, entry["name"])
        outputs[f"files{number}"] = "\n".join(_in_checkout(p, repo) for p in entry["key_files"])
    return outputs


def report(cfg, steps):
    """One line per entry from the restore steps' results. A miss is never an error."""
    lines, restored = [], 0
    for number, entry in enumerate(cfg["cache"], 1):
        step = steps.get(f"cache{number}") or {}
        outputs = step.get("outputs") or {}
        key, matched = outputs.get("cache-primary-key"), outputs.get("cache-matched-key")
        if step.get("outcome") != "success":
            lines.append(f"{entry['name']}: not restored, the restore step failed")
            continue
        if matched:
            restored += 1
        if matched and outputs.get("cache-hit") == "true":
            lines.append(f"{entry['name']}: restored {matched}")
        elif matched:
            lines.append(f"{entry['name']}: restored {matched}, an older cache than {key}")
        else:
            lines.append(f"{entry['name']}: missed, no cache for {key} or an older key")
    missed = len(lines) - restored
    lines.append(f"Caches: {restored} restored, {missed} missed. Setup installs what is missing.")
    if missed:
        lines.append(f"The base branch's own workflow saves them under these keys: {DOCS}")
    return lines
