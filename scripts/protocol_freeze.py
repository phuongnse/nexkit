#!/usr/bin/env python3
"""Copy the protocol's schemas to `tests/golden/protocol/`, the copy of a released minor version.

    protocol_freeze.py [--out FOLDER]

The release pull request of 7.0, and each later one that raises `protocol.MINOR`, runs it
(docs/delivery.md#releases). The unit tests compare the tree's schemas with the copy: they allow a
new optional property whose `x-since` is above the copy's minor version and a changed `$comment`,
and refuse any other change (docs/engine.md#versions). `state.schema.json` may change only when
its schema version goes up.

When `FOLDER` already holds a copy of the same major version, the script first compares the tree
with it. It prints what the copy does not allow, exits 1 and writes nothing. Otherwise it replaces
the copy and exits 0.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nexkit import protocol  # noqa: E402

DEFAULT_OUT = ROOT / "tests" / "golden" / "protocol"
VERSION_FILE = "version.json"
STATE = "state.schema.json"
SINCE = re.compile(r"1\.(\d+)")


def files(folder):
    """The schema files under `folder`, outside `examples/`, by their path in it."""
    return sorted(
        path
        for path in Path(folder).rglob("*.schema.json")
        if "examples" not in path.relative_to(folder).parts
    )


def read(folder):
    """Each schema file under `folder`, parsed with its annotations, by its POSIX path."""
    return {
        path.relative_to(folder).as_posix(): json.loads(path.read_text(encoding="utf-8"))
        for path in files(folder)
    }


def _plain(value):
    if isinstance(value, dict):
        return {key: _plain(sub) for key, sub in value.items() if key != "$comment"}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _version(schema):
    try:
        return schema["properties"]["schemaVersion"]["const"]
    except (KeyError, TypeError):
        return None


def _since(schema, minor):
    value = schema.get("x-since") if isinstance(schema, dict) else None
    match = SINCE.fullmatch(value) if isinstance(value, str) else None
    return bool(match) and int(match.group(1)) > minor


def _walk(old, new, where, found, minor, strict):
    """Add to `found` what differs between two nodes of a schema. Unless `strict`, a property that
    only `new` has is allowed when it is optional and its `x-since` is above `minor`."""
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(old.keys() | new.keys()):
            here = f"{where}/{key}"
            if key not in new:
                found.append(f"{here} was removed")
            elif key not in old:
                found.append(f"{here} was added")
            elif key == "properties" and isinstance(old[key], dict) and isinstance(new[key], dict):
                _properties(old, new, where, found, minor, strict)
            else:
                _walk(old[key], new[key], here, found, minor, strict)
    elif isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        if any(isinstance(item, (dict, list)) for item in old):
            for index, (before, after) in enumerate(zip(old, new, strict=True)):
                _walk(before, after, f"{where}/{index}", found, minor, strict)
        elif old != new:
            found.append(f"{where} changed")
    elif old != new:
        found.append(f"{where} changed")


def _properties(old, new, where, found, minor, strict):
    required = new.get("required")
    required = required if isinstance(required, list) else []
    here = f"{where}/properties"
    for name in sorted(old["properties"].keys() | new["properties"].keys()):
        if name not in new["properties"]:
            found.append(f"{here}/{name} was removed")
        elif name not in old["properties"]:
            allowed = not strict and name not in required and _since(new["properties"][name], minor)
            if not allowed:
                found.append(f"{here}/{name} was added without an x-since above 1.{minor}")
        else:
            at = f"{here}/{name}"
            _walk(old["properties"][name], new["properties"][name], at, found, minor, strict)


def problems(old, new, minor):
    """What the schemas `new` change that the copy `old` of minor version `minor` does not allow,
    as short sentences. Both are `read` results."""
    old, new = _plain(old), _plain(new)
    found = []
    for name in sorted(old.keys() | new.keys()):
        if name not in new:
            found.append(f"{name} was removed")
        elif name not in old:
            found.append(f"{name} was added")
        elif name == STATE:
            before, after = _version(old[name]), _version(new[name])
            raised = isinstance(before, int) and isinstance(after, int) and after > before
            if not raised:
                _walk(old[name], new[name], name + "#", found, minor, True)
        else:
            _walk(old[name], new[name], name + "#", found, minor, False)
    return found


def write(out):
    """Replace the copy in `out` with the tree's schemas and their version."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    for path in files(out):
        path.unlink()
    (out / VERSION_FILE).unlink(missing_ok=True)
    for folder in sorted((p for p in out.rglob("*") if p.is_dir()), reverse=True):
        if not any(folder.iterdir()):
            folder.rmdir()
    for path in files(protocol.ROOT):
        target = out / path.relative_to(protocol.ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
    version = {"major": protocol.VERSION, "minor": protocol.MINOR}
    (out / VERSION_FILE).write_text(json.dumps(version, indent=2) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Copy the protocol's schemas for the tests.")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="the folder of the copy")
    out = Path(parser.parse_args(argv).out)
    marker = out / VERSION_FILE
    if marker.exists():
        copy = json.loads(marker.read_text(encoding="utf-8"))
        if copy["major"] == protocol.VERSION:
            found = problems(read(out), read(protocol.ROOT), copy["minor"])
            if found:
                print(f"The schemas break the copy of {copy['major']}.{copy['minor']}:")
                for line in found:
                    print(f"- {line}")
                return 1
    write(out)
    print(f"Wrote {out} as {protocol.VERSION}.{protocol.MINOR}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
