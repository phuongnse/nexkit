"""The engine's protocol: the JSON Schemas of its events, actions and their results, each
stage's result and the state document, as docs/engine.md describes them.

The schemas are the contract every host and runner reads, so they live as JSON files in
`v1/`, and NexKit reads its stages' result schemas from them too. `$comment` keys explain a
schema to people; `load` leaves them out, so a schema given to an agent holds none.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path, PurePosixPath

from ..schema import problems

# The protocol's version. The state document has a schema version of its own
# (`state.schema.json`), so that a state can move on without a new protocol.
VERSION = 1
ROOT = Path(__file__).parent / f"v{VERSION}"


def _plain(value):
    if isinstance(value, dict):
        return {key: _plain(sub) for key, sub in value.items() if key != "$comment"}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


@functools.cache
def _read(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def load(name):
    """The schema in `v1/<name>`, such as "results/plan.schema.json", without `$comment`."""
    return _plain(_read(name))


def _refs(ref, base):
    path, _, pointer = ref.partition("#")
    name = str(PurePosixPath(base).parent / path) if path else base
    name = str(PurePosixPath(*_normal(PurePosixPath(name).parts)))
    schema = _read(name)
    for part in filter(None, pointer.split("/")):
        schema = schema[part]
    return schema, name


def _normal(parts):
    out = []
    for part in parts:
        if part == "..":
            out.pop()
        elif part != ".":
            out.append(part)
    return out


def check(value, name, where="value"):
    """The ways `value` breaks the schema in `v1/<name>`, as short sentences."""
    return problems(value, _read(name), where, _refs, name)


def names(folder):
    """The names in a folder of `v1/`, such as the event kinds in "events"."""
    return sorted(
        path.name.removesuffix(".schema.json")
        for path in (ROOT / folder).glob("*.schema.json")
        if not path.name.endswith(".result.schema.json")
    )
