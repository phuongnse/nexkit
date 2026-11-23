"""Pure text helpers that the prompts and the GitHub host share: how a text is clipped and
fenced, what a prompt shows of a failing check's output, and the limits a prompt names.

The host's modules import them from here, so each rule has one definition and the engine
needs none of the host's code.
"""

from __future__ import annotations

import re

# GitHub keeps at most this many characters in an issue's body or a comment.
TEXT_LIMIT = 65_536
# The most parts a split may have. The plan prompt says so, and autopilot files a split
# whole or not at all: a plan with more parts is asked again for fewer, larger ones, which
# may split again in turn (see `autopilot.MAX_DEPTH`).
MAX_PARTS = 10
# What a check's result keeps of its output: its last this many characters.
OUTPUT_TAIL = 12_000
# Which shard of a sharded check a job runs, as `k/N`.
SHARD = "NEXKIT_SHARD"
# A line of a check's output that reports a failure, for `failure_signature`.
FAILURE_LINE = re.compile(r"fail|error|✘|✗", re.IGNORECASE)
# What a check's result and a prompt keep of the lines that report a failure before the end
# of its output: this many, each at most this many characters.
FAILURE_LINES = 20
FAILURE_LINE_WIDTH = 300


def clip(text, limit=4000):
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "\n[truncated]"


def fence(text, char="`"):
    """A code fence longer than any run of `char` in the text."""
    runs = re.findall(f"{re.escape(char)}+", text)
    return char * max([3, *(len(run) + 1 for run in runs)])


def checklist_item(item):
    """The text of a `review.checklist` item: a string, or {"text", "person"}."""
    return item["text"] if isinstance(item, dict) else item


def person_decides(item):
    """Whether only a person decides a `review.checklist` item that is not met, such as one
    that accepts a risk: NexKit starts no fix round for it, and merges on their approval."""
    return isinstance(item, dict) and item.get("person") is True


def since_text(run):
    """Whether a newer run of a named run's workflow passed on the default branch, in words."""
    name, branch, passed = run["workflow"], run["branch"], run.get("passed_since")
    if not passed:
        return f"no newer run of `{name}` has passed on `{branch}` since"
    commit = f" at {passed['sha'][:7]}" if passed.get("sha") else ""
    link = f", {passed['url']}" if passed.get("url") else ""
    return f"run {passed['id']} of `{name}` passed on `{branch}` since{commit}{link}"


def shown_command(check):
    """A check's command as prompts show it: a shard's with its NEXKIT_SHARD, so an agent
    runs that shard alone."""
    return f"{SHARD}={check['shard']} {check['run']}" if check.get("shard") else check["run"]


def _fenced(text):
    return f"{fence(text)}\n{text}\n{fence(text)}"


def failing_lines(text):
    """The lines of `text` that report a failure (FAILURE_LINE), stripped, each at most
    FAILURE_LINE_WIDTH characters."""
    return [
        line if len(line) <= FAILURE_LINE_WIDTH else line[:FAILURE_LINE_WIDTH] + " […]"
        for line in (raw.strip() for raw in text.splitlines())
        if FAILURE_LINE.search(line)
    ]


def output_text(check, tail, lines, keep=None):
    """What a prompt shows of a failing `check`'s output: the whole output when it has at
    most `tail` characters and no earlier `failure_lines`; else the first `lines` lines
    that report a failure, those its result recorded from before the output it kept, then
    those of the kept output before its last `tail` characters, then that end, from the
    start of a line. `keep(text)` keeps what NexKit kept of the output whole, in a file
    outside the working tree, and returns its path, or "": the prompt names the file. A
    result without `failure_lines`, such as one an older NexKit saved, shows the same way."""
    raw = check.get("output") or ""
    output = raw.rstrip()
    earlier = [str(line) for line in check.get("failure_lines") or []]
    # The state comment kept less of it, to fit GitHub's limit (`state.fit_state`).
    cut = bool(check.get("output_cut"))
    if len(output) <= tail and not earlier and not cut:
        return f"Output:\n\n{_fenced(output)}"
    if cut and not output and not earlier:
        return (
            "NexKit left its output out of the state comment, to fit GitHub's limit on a "
            "comment's length; the run's links lead to it."
        )
    end = output[-tail:]
    if len(output) > tail and output[-tail - 1] != "\n" and "\n" in end:
        end = end[end.index("\n") + 1 :]
    found = earlier + failing_lines(output[: len(output) - len(end)])
    shown = found[:lines]
    text = output
    if earlier:
        text = (
            "Lines before the output that NexKit kept that report a failure or an error:\n\n"
            + "\n".join(earlier)
            + f"\n\nThe output NexKit kept, its last {len(raw):,} characters:\n\n{output}"
        )
    path = keep(text) if keep else ""
    where = ""
    if path and (earlier or cut or len(raw) >= OUTPUT_TAIL):
        also = ", with the lines before them that report a failure" if earlier else ""
        where = f"; the output NexKit kept (its last {len(raw):,} characters{also}) is in `{path}`"
    elif path:
        where = f"; the whole output is in `{path}`"
    parts = []
    if shown:
        partly = cut or len(found) > len(shown) or len(earlier) >= FAILURE_LINES
        which = f"the first {len(shown)}" if partly else "all"
        parts.append(
            f"Lines that report a failure or an error ({which}):\n\n" + _fenced("\n".join(shown))
        )
    parts.append(
        f"End of output (the last {len(end):,} of {len(output):,} characters{where}):\n\n"
        + _fenced(end)
    )
    return "\n\n".join(parts)
