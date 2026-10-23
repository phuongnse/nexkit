"""When to resume a run that stopped at the account's usage limit.

Each runtime recognises its own CLI's limit message and sets `Outcome.limit` to
{"message": the line that says so, "reset": UTC datetime or None}. A run that stops there
is paused, not failed, and resumes after the reset.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

# Usage limits reset within a week; paused runs older than this are not resumed.
PAUSE_WINDOW = timedelta(days=8)


def schedule(limit, now=None, retry_minutes=60):
    """When to resume after a runtime's `Outcome.limit`, and whether the CLI said so.
    Without a reset time, NexKit tries again after `retry_minutes`."""
    now = now or datetime.now(UTC)
    when = limit["reset"] or now + timedelta(minutes=retry_minutes)
    return {
        "message": limit["message"],
        "resume_at": iso(when),
        "reset_known": bool(limit["reset"]),
    }


def iso(when):
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text):
    try:
        return datetime.strptime(str(text), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def shown(text):
    """`2026-10-11T04:40:00Z` as people read it: `04:40 UTC on 2026-10-11`."""
    when = parse_iso(text)
    return f"{when:%H:%M} UTC on {when:%Y-%m-%d}" if when else str(text)
