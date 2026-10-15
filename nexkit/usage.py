"""Recognise when an agent stopped at the account's usage limit, and when the limit resets.

Claude Code says so in plain text, for example `You've hit your session limit · resets
4:40am (UTC)`, and older versions as `Claude AI usage limit reached|1760000000`. A runtime
passes the texts its CLI wrote about a failed run. A run that stops there is paused, not
failed, and resumes after the reset.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Text that names a limit which resets. A credit balance that is too low never resets.
LIMIT = re.compile(
    r"usage limit|session limit|weekly limit|\d+-hour limit|limit reached"
    r"|(?:hit|reached) your [\w .-]{0,40}limit",
    re.I,
)
EPOCH = re.compile(r"limit reached\|(\d{9,11})\b", re.I)
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
RESET = re.compile(
    r"\breset(?:s)?\s+(?:at\s+|on\s+)?"
    r"(?:(?P<month>[A-Za-z]{3})[a-z]*\.?\s+(?P<day>\d{1,2}),?\s+(?:at\s+)?)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>[ap]\.?m\.?)?"
    r"(?:\s*\((?P<zone>[^)]{1,40})\))?",
    re.I,
)


def _zone(name):
    try:
        return ZoneInfo(name.strip()) if name else UTC
    except (ZoneInfoNotFoundError, ValueError):
        return UTC


def reset_time(text, now):
    """The UTC time at which the limit in `text` resets, or None when it gives none."""
    epoch = EPOCH.search(text)
    if epoch:
        return datetime.fromtimestamp(int(epoch.group(1)), UTC)
    match = RESET.search(text)
    if not match:
        return None
    hour, minute = int(match["hour"]), int(match["minute"] or 0)
    ampm = (match["ampm"] or "").lower().replace(".", "")
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59 or (not ampm and not match["minute"]):
        return None
    zone = _zone(match["zone"])
    local = now.astimezone(zone)
    if match["month"]:
        month = match["month"][:3].lower()
        if month not in MONTHS:
            return None
        try:
            when = local.replace(
                month=MONTHS.index(month) + 1,
                day=int(match["day"]),
                hour=hour,
                minute=minute,
                second=0,
                microsecond=0,
            )
        except ValueError:
            return None
        if when < local - timedelta(days=1):
            when = when.replace(year=when.year + 1)
    else:
        when = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if when <= local:
            when += timedelta(days=1)
    return when.astimezone(UTC)


def limit_reached(texts, now=None, resets_at=None):
    """When one of `texts` says the agent hit a usage limit: the line that says so and when
    the limit resets (UTC), or None for the reset when no text says. Otherwise None.

    `resets_at` is a reset time in Unix seconds that the CLI reported in its event stream;
    it wins over the text."""
    now = now or datetime.now(UTC)
    for text in texts:
        # The CLI's message is short and says it first; longer text is something else.
        text = str(text or "").strip()
        line = text.splitlines()[0][:300] if text else ""
        if not LIMIT.search(line) or len(text) > 1000:
            continue
        when = None
        if isinstance(resets_at, (int, float)) and resets_at > 0:
            when = datetime.fromtimestamp(resets_at, UTC)
        when = when or reset_time(text, now)
        if when is not None and when <= now - timedelta(hours=1):
            when = None
        return {"message": line, "reset": when}
    return None


def schedule(limit, now=None, retry_minutes=60):
    """When to resume after `limit` from `limit_reached`, and whether the CLI said so.
    Without a reset time, NexKit tries again after `retry_minutes`."""
    now = now or datetime.now(UTC)
    when = limit["reset"] or now + timedelta(minutes=retry_minutes)
    return {
        "message": limit["message"],
        "resume_at": iso(when),
        "reset_known": bool(limit["reset"]),
    }


def usage_limit(texts, now=None, resets_at=None, retry_minutes=60):
    """When one of `texts` says the agent hit a usage limit: the line that says so, when to
    resume (UTC) and whether that time came from the CLI. Otherwise None."""
    limit = limit_reached(texts, now, resets_at)
    return schedule(limit, now, retry_minutes) if limit else None


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
