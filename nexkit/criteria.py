"""Acceptance criteria belong to the approved specification, never the reviewer."""

from __future__ import annotations

import re

from .common import digest
from .policy import require

HEADER = re.compile(r"^(#{1,6})\s+Acceptance criteria\s*#*\s*$", re.IGNORECASE)
ITEM = re.compile(r"^(?:[-*+]\s+|\d+[.)]\s+)(?:\[([A-Za-z][A-Za-z0-9_-]{0,47})\]\s+)?(.+)$")


def specification_criteria(issue):
    """Read an explicit Markdown list, excluding the quoted original request."""
    body = issue.get("body") or ""
    body = body.split("\n<!-- nexkit:spec -->\n", 1)[-1]
    sections, items, level, fenced = 0, [], None, None
    for line in body.splitlines():
        if match := re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line):
            marker, suffix = match.groups()
            if fenced is None:
                fenced = marker
            elif marker[0] == fenced[0] and len(marker) >= len(fenced) and not suffix.strip():
                fenced = None
            continue
        if fenced:
            continue
        if match := HEADER.fullmatch(line):
            sections += 1
            require(sections == 1, "Specification has multiple acceptance criteria sections")
            level = len(match[1])
            continue
        if level is None:
            continue
        if match := re.match(r"^(#{1,6})\s", line):
            if len(match[1]) <= level:
                level = None
                continue
        if match := ITEM.fullmatch(line):
            require(match[2].strip(), "Acceptance criterion text must not be empty")
            require(
                not (match[1] is None and re.fullmatch(r"\[[^\]]*\]", match[2].strip())),
                "Acceptance criterion needs text after its ID",
            )
            items.append({"id": match[1], "text": match[2].strip()})
        elif line.strip():
            require(items, "Acceptance criteria must be an explicit Markdown list")
            items[-1]["text"] += "\n" + line.rstrip()
    require(fenced is None, "Specification has an unclosed code fence")
    require(items and len(items) <= 200, "Specification needs a bounded Acceptance criteria list")
    require(
        sum(len(item["text"].encode()) for item in items) <= 24000,
        "Acceptance criteria exceed 24 KB",
    )
    for item in items:
        if item["id"] is None:
            item["id"] = "AC-" + digest(item["text"])[:16]
    require(len({item["id"] for item in items}) == len(items), "Duplicate acceptance criterion")
    return items


def coverage(candidate, result):
    expected = candidate.get("criteria")
    require(
        isinstance(expected, list) and expected, "Candidate has no approved acceptance criteria"
    )
    identifiers = {item["id"] for item in expected}
    require(len(identifiers) == len(expected), "Candidate has duplicate acceptance criteria")
    reported = [item["criterion"] for item in result["acceptance"]]
    require(len(reported) == len(set(reported)), "Review repeats an acceptance criterion")
    # Additional issue completion entries are checked against the approved
    # completion plan separately. They cannot satisfy a main requirement ID.
    criteria = [
        name for name in reported if not re.fullmatch(r"Issue #[1-9][0-9]* completion", name)
    ]
    completion = set(reported) - set(criteria)
    require(
        completion <= set(candidate.get("completion_criteria", [])),
        "Review invents an issue completion criterion",
    )
    require(set(criteria) <= identifiers, "Review reports an unknown acceptance criterion")
    if result["verdict"] == "approve":
        require(set(criteria) == identifiers, "Review omits approved acceptance criteria")
