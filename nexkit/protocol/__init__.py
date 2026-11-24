"""The engine's protocol: the JSON Schemas of its events, actions and their results, each
stage's result and the state document, as docs/engine.md describes them.

The schemas are the contract every host and runner reads, so they live as JSON files in
`v1/`, and NexKit reads its stages' result schemas from them too. Each file names its dialect
(`$schema`) and its URI (`$id`, under `BASE`), so that a standard validator in any language
resolves their relative `$ref`s; `$comment` keys explain a schema to people. `load` leaves
these three out, so a schema given to an agent is the one NexKit 6 gave.
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
# The JSON Schema dialect of every file, and the URI each file's `$id` starts with.
DIALECT = "https://json-schema.org/draft/2020-12/schema"
BASE = f"https://raw.githubusercontent.com/phuongnse/nexkit/main/nexkit/protocol/v{VERSION}/"
# The keys that describe a file rather than a value, which `load` leaves out.
ANNOTATIONS = ("$comment", "$schema", "$id")


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


def check(value, name, where="value"):
    """The ways `value` breaks the schema in `v1/<name>`, as short sentences. `name` may point
    into the file, such as "results/checklist.schema.json#/$defs/review"."""
    name, _, pointer = name.partition("#")
    return problems(value, _at(_read(name), pointer), where, _refs, name)


def names(folder):
    """The names in a folder of `v1/`, such as the event kinds in "events"."""
    return sorted(
        path.name.removesuffix(".schema.json")
        for path in (ROOT / folder).glob("*.schema.json")
        if not path.name.endswith(".result.schema.json")
    )
