"""The engine's protocol: the JSON Schemas of its events, actions and their results, each
stage's result and the state document, as docs/engine.md describes them.

The schemas are the contract every host and runner reads, so they live as JSON files in
`v1/`, and NexKit reads its stages' result schemas from them too. Each file names its dialect
(`$schema`) and its URI (`$id`, under `BASE`), so that a standard validator in any language
resolves their relative `$ref`s; `$comment` keys explain a schema to people, and `x-since` names
the minor version that added a field; a new value is a new major (docs/engine.md#versions).
`load` leaves these out, so an agent gets only the keywords that check its result.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path, PurePosixPath

from ..schema import problems

# The protocol's version. The state document has a schema version of its own
# (`state.schema.json`), so that a state can move on without a new protocol.
VERSION = 1
# The most problems `check` tells. A request that breaks the protocol in many places, such as
# a large array of wrong items, is refused as fast and as briefly as one that breaks it once.
LIMIT = 20
# The protocol's minor version: the one place it lives. Each field added after 1.0
# carries `"x-since": "1.<minor>"`, and `describe` reports it.
MINOR = 0
ROOT = Path(__file__).parent / f"v{VERSION}"
# The JSON Schema dialect of every file, and the URI each file's `$id` starts with.
DIALECT = "https://json-schema.org/draft/2020-12/schema"
BASE = f"https://github.com/phuongnse/nexkit/protocol/v{VERSION}/"
# The keys that describe a file rather than a value, which `load` leaves out.
ANNOTATIONS = ("$comment", "$schema", "$id", "x-since")


def _plain(value):
    if isinstance(value, dict):
        return {key: _plain(sub) for key, sub in value.items() if key not in ANNOTATIONS}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


@functools.cache
def _read(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def load(name):
    """The schema in `v1/<name>`, such as "results/plan.schema.json", without the keys that
    describe the file (`ANNOTATIONS`)."""
    return _plain(_read(name))


def _refs(ref, base):
    path, _, pointer = ref.partition("#")
    name = str(PurePosixPath(base).parent / path) if path else base
    name = "/".join(_normal(PurePosixPath(name).parts, ref))
    return _at(_read(name), pointer), name


def _at(schema, pointer):
    for part in filter(None, pointer.split("/")):
        schema = schema[part]
    return schema


def _normal(parts, ref):
    out = []
    for part in parts:
        if part == ".." and not out:
            raise ValueError(f"{ref} leaves v{VERSION}/")
        if part == "..":
            out.pop()
        elif part != ".":
            out.append(part)
    return out


def finding_location(file, line):
    """A review finding's `location` from the `file` and `line` that a version 1 document
    and a host of files keep: `file:line`, `file` alone when the line is 0, and an empty
    string when there is no file."""
    if not file:
        return ""
    return f"{file}:{line}" if line else str(file)


def _located(finding):
    if not isinstance(finding, dict) or "location" in finding:
        return finding
    kept = {key: value for key, value in finding.items() if key not in ("file", "line")}
    place = finding_location(finding.get("file"), finding.get("line"))
    return {**kept, "location": place}


def upgrade(state):
    """A state document as the current protocol reads it. One of schema version 1 moves to
    version 2: each finding of the last review has a `location` in place of `file` and `line`
    (`finding_location`), and the plan's `protectedPaths` is `protected`. Before 1.0, one
    that still holds `used.usd`, which NexKit no longer keeps, is read without it. Any other
    value comes back as it is, for `check` to refuse."""
    if not isinstance(state, dict):
        return state
    used = state.get("used")
    if isinstance(used, dict) and "usd" in used:
        state = {**state, "used": {key: value for key, value in used.items() if key != "usd"}}
    if state.get("schemaVersion") != 1:
        return state
    state = {**state, "schemaVersion": 2}
    plan = state.get("plan")
    if isinstance(plan, dict) and "protectedPaths" in plan:
        kept = {key: value for key, value in plan.items() if key != "protectedPaths"}
        state["plan"] = {**kept, "protected": plan["protectedPaths"]}
    rounds = state.get("rounds")
    review = rounds.get("lastReview") if isinstance(rounds, dict) else None
    if isinstance(review, dict) and isinstance(review.get("findings"), list):
        found = [_located(item) for item in review["findings"]]
        state["rounds"] = {**rounds, "lastReview": {**review, "findings": found}}
    return state


def check(value, name, where="value"):
    """The ways `value` breaks the schema in `v1/<name>`, as short sentences: at most `LIMIT`,
    then "and more". `name` may point into the file, such as
    "results/checklist.schema.json#/$defs/review"."""
    name, _, pointer = name.partition("#")
    found = problems(value, _at(_read(name), pointer), where, _refs, name, LIMIT)
    return found if len(found) <= LIMIT else [*found[:LIMIT], "and more"]


def names(folder):
    """The names in a folder of `v1/`, such as the event kinds in "events"."""
    return sorted(
        path.name.removesuffix(".schema.json")
        for path in (ROOT / folder).glob("*.schema.json")
        if not path.name.endswith(".result.schema.json")
    )
