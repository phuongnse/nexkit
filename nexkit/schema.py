"""Check an agent's structured result against its stage schema.

Agent output is untrusted, and runtimes enforce schemas differently, so NexKit checks the
result itself before anything uses it. The same check reads the engine's protocol
(`nexkit/protocol/`). Only the parts of JSON Schema that those schemas use are supported:
`type` (one name or a list), `enum`, `const`, `properties`, `required`, `items`,
`additionalProperties`, `anyOf` and `$ref`. Keys that a schema does not name are left as they
are unless `additionalProperties` is false.
"""

from __future__ import annotations

import string

# What an agent may wrap an empty string in by mistake, such as `""`.
QUOTES = "\"'`\u201c\u201d\u2018\u2019"

TYPES = {
    "null": type(None),
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "integer": int,
    "number": (int, float),
}


def problems(value, schema, where="result", refs=None, base=None):
    """The ways `value` breaks `schema`, as short sentences. Empty when it fits.

    `refs(ref, base)` answers the schema a `$ref` names and the base of that schema's own
    references, for schemas in files; `base` is the file `schema` came from."""
    if "$ref" in schema:
        if refs is None:
            raise ValueError(f"{where}: no way to read {schema['$ref']}")
        target, target_base = refs(schema["$ref"], base)
        return problems(value, target, where, refs, target_base)
    if "anyOf" in schema:
        options = [problems(value, sub, where, refs, base) for sub in schema["anyOf"]]
        if all(options):
            return min(options, key=len)
        return []
    found = []
    kind = schema.get("type")
    if kind:
        kinds = kind if isinstance(kind, list) else [kind]
        if not any(_is(value, name) for name in kinds):
            names = " or ".join(f"{'an' if name[0] in 'aeiou' else 'a'} {name}" for name in kinds)
            return [f"{where} must be {names}"]
    if "const" in schema and value != schema["const"]:
        found.append(f"{where} must be {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        allowed = ", ".join(repr(item) for item in schema["enum"])
        found.append(f"{where} must be one of {allowed}")
    if isinstance(value, dict):
        for key in schema.get("required", ()):
            if key not in value:
                found.append(f"{where}.{key} is missing")
        properties = schema.get("properties", {})
        for key, sub in properties.items():
            if key in value:
                found += problems(value[key], sub, f"{where}.{key}", refs, base)
        if schema.get("additionalProperties") is False:
            found += [f"{where}.{key} is not allowed" for key in value if key not in properties]
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            found += problems(item, schema["items"], f"{where}[{index}]", refs, base)
    return found


def _is(value, name):
    if name == "null":
        return value is None
    if isinstance(value, bool) and name in ("integer", "number"):
        return False
    return isinstance(value, TYPES[name])


def filled(value):
    """A free-text field of a result, stripped, or "" when it holds no text once quotes and
    whitespace are removed: an agent sometimes writes an empty string as `""`."""
    found = str(value or "").strip()
    return found if found.strip(QUOTES + string.whitespace) else ""


def strict(schema):
    """A copy for structured-output modes that need every object closed and every property
    required, and a type next to each enum. NexKit's schemas already require every property."""
    schema = dict(schema)
    if (
        "enum" in schema
        and "type" not in schema
        and all(isinstance(v, str) for v in schema["enum"])
    ):
        schema["type"] = "string"
    if "properties" in schema:
        properties = {key: strict(sub) for key, sub in schema["properties"].items()}
        schema.update(properties=properties, required=list(properties), additionalProperties=False)
    if "items" in schema:
        schema["items"] = strict(schema["items"])
    return schema
