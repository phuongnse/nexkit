"""Path globs for a check's `unaffected_by`: the paths it never reads.

`*` matches any characters but `/`, `?` one of them, `[abc]` and `[!abc]` one of a set, and
`**`, only as a whole segment, any number of folders. A pattern that starts with `!` takes
paths back out; the last pattern that matches a path decides, as in the `paths` filters of
GitHub Actions. A path that no pattern matches is one the check reads.
"""

from __future__ import annotations

import re


class GlobError(ValueError):
    pass


def _segment(text):
    """The regular expression for one path segment without `**`."""
    out, index = [], 0
    while index < len(text):
        char = text[index]
        index += 1
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            end = text.find("]", index)
            if end == -1:
                raise GlobError("has a `[` without its `]`")
            body = text[index:end]
            negated = body.startswith("!")
            body = body[1:] if negated else body
            if not body:
                raise GlobError("has an empty `[]`")
            # Each character as itself, but `-` between two, which gives a range.
            escaped = "".join(c if c == "-" else re.escape(c) for c in body)
            out.append(f"[{'^/' if negated else ''}{escaped}]")
            index = end + 1
        elif char == "]":
            raise GlobError("has a `]` without its `[`")
        else:
            out.append(re.escape(char))
    return "".join(out)


def compile_glob(pattern):
    """(compiled, negated) for one pattern, or GlobError saying why it is not one."""
    if not isinstance(pattern, str) or not pattern.strip() or "\n" in pattern:
        raise GlobError("is empty")
    negated = pattern.startswith("!")
    body = pattern[1:] if negated else pattern
    if not body:
        raise GlobError("is only `!`")
    if body.startswith("/"):
        raise GlobError("starts with `/`: paths are relative to the repository root")
    if body.endswith("/"):
        raise GlobError(f"ends with `/`: use `{body}**` for everything under the folder")
    segments = body.split("/")
    out = []
    for position, segment in enumerate(segments):
        last = position == len(segments) - 1
        if segment in ("", ".", ".."):
            raise GlobError("has an empty, `.` or `..` segment")
        if segment == "**":
            # Any number of folders; at the end, everything under them.
            out.append(".*" if last else "(?:[^/]+/)*")
            continue
        if "**" in segment:
            raise GlobError("has `**` inside a segment: `**` must be a whole segment")
        out.append(_segment(segment) + ("" if last else "/"))
    try:
        compiled = re.compile("".join(out))
    except re.error as exc:
        raise GlobError(f"is not a valid glob ({exc})") from None
    return compiled, negated


def compile_globs(patterns):
    """The compiled list for `unaffected`, or GlobError naming the first pattern that is not
    one."""
    compiled = []
    for pattern in patterns:
        try:
            compiled.append(compile_glob(pattern))
        except GlobError as exc:
            raise GlobError(f"'{pattern}' {exc}") from None
    if compiled and compiled[0][1]:
        raise GlobError(f"'{patterns[0]}' starts with `!` but is first: it takes nothing out")
    return compiled


def unaffected(paths, patterns):
    """Whether `patterns`, a check's `unaffected_by`, list every path of `paths`: for each,
    the last pattern that matches it decides."""
    compiled = compile_globs(patterns)

    def listed(path):
        found = False
        for regex, negated in compiled:
            if regex.fullmatch(path):
                found = not negated
        return found

    return all(listed(path) for path in paths)
