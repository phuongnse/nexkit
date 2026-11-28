"""The facts about each stage that the host and the engine must agree on: the state that runs
it, its access, the texts its prompt is written from, and the limits of a diff and a commit.
Standard library only, as `nexkit.textutil` is."""

from __future__ import annotations

import re

# The state that runs each stage's agent.
RUNS = {"plan": "planning", "implement": "working", "fix": "fixing", "review": "reviewing"}
ACCESS = {"triage": "none", "plan": "read", "review": "read", "implement": "write", "fix": "write"}
# The texts each stage's prompt is written from (`prompt.write`), by the kinds of runAgent's
# `inputs`: the host fetches them for the `prompt` call.
INPUTS = {
    "triage": ["item", "discussion", "notes", "previousPlan"],
    "plan": [
        "item",
        "discussion",
        "notes",
        "previousPlan",
        "linked",
        "evidence",
        "parts",
        "workspace",
    ],
    "implement": ["item", "discussion", "notes", "plan", "linked", "evidence", "workspace"],
    "fix": [
        "item",
        "discussion",
        "notes",
        "plan",
        "checks",
        "linked",
        "evidence",
        "diff",
        "workspace",
    ],
    "review": [
        "item",
        "discussion",
        "plan",
        "checks",
        "linked",
        "diff",
        "workspace",
    ],
}
MAX_DIFF = 80_000
COMMIT = re.compile(r"[0-9a-f]{40}")
