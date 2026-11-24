"""Check an agent's structured result against its stage schema.

Agent output is untrusted, and runtimes enforce schemas differently, so NexKit checks the
result itself before anything uses it. The same check reads the engine's protocol
(`nexkit/protocol/`). Only the parts of JSON Schema that those schemas use are supported: `type`
(one name or a list), `enum`, `const`, `properties`, `required`, `items`, `maxItems`, `maxLength`,
`minimum`, `maximum`, `additionalProperties`, `anyOf` and `$ref`, each as JSON Schema 2020-12 reads
it: the keywords beside a `$ref` or an `anyOf` apply too, and `const` and `enum` compare JSON
values, so `true` is not `1`. Keys that a schema does not name are left as they are unless
`additionalProperties` is false. One difference is deliberate: an integer is written without a
fraction, so `1.0` is not one, because NexKit counts and indexes with integers.
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


def problems(value, schema, where="result", refs=None, base=None, limit=None):
    """The ways `value` breaks `schema`, as short sentences. Empty when it fits.

    `refs(ref, base)` answers the schema a `$ref` names and the base of that schema's own
    references, for schemas in files; `base` is the file `schema` came from. With a `limit`,
    the check of an object's properties and an array's items stops once it found more problems
    than that, so a large value that breaks the schema costs little; the problems found then
    are some of them, and more than `limit`."""
    found = []
    if "$ref" in schema:
        if refs is None:
            raise ValueError(f"{where}: no way to read {schema['$ref']}")
        target, target_base = refs(schema["$ref"], base)
        found += problems(value, target, where, refs, target_base, limit)
    if "anyOf" in schema:
        options = []
        for sub in schema["anyOf"]:
            options.append(problems(value, sub, where, refs, base, limit))
            if not options[-1]:
                break  # One option fits, so the others change nothing.
        if all(options):
            # The option a value names by a constant, such as an action's `kind`, says best
            # what is wrong with it; otherwise the one with the fewest problems.
            named = [_names(value, sub, refs, base) for sub in schema["anyOf"]]
            ranked = zip(named, options, strict=True)
            found += min(ranked, key=lambda pair: (not pair[0], len(pair[1])))[1]
    kind = schema.get("type")
    if kind:
        kinds = kind if isinstance(kind, list) else [kind]
        if not any(_is(value, name) for name in kinds):
            names = " or ".join(f"{'an' if name[0] in 'aeiou' else 'a'} {name}" for name in kinds)
            return [*found, f"{where} must be {names}"]
    if "const" in schema and not _same(value, schema["const"]):
        found.append(f"{where} must be {schema['const']!r}")
    if _is(value, "number") and "minimum" in schema and value < schema["minimum"]:
        found.append(f"{where} must be at least {schema['minimum']}")
    if _is(value, "number") and "maximum" in schema and value > schema["maximum"]:
        found.append(f"{where} must be at most {schema['maximum']}")
    if "enum" in schema and not any(_same(value, item) for item in schema["enum"]):
        allowed = ", ".join(repr(item) for item in schema["enum"])
        found.append(f"{where} must be one of {allowed}")
    if isinstance(value, dict):
        for key in schema.get("required", ()):
            if key not in value:
                found.append(f"{where}.{key} is missing")
        properties = schema.get("properties", {})
        for key, sub in properties.items():
            if key in value:
                found += problems(value[key], sub, f"{where}.{key}", refs, base, limit)
                if limit is not None and len(found) > limit:
                    return found
        if schema.get("additionalProperties") is False:
            found += [f"{where}.{key} is not allowed" for key in value if key not in properties]
    if isinstance(value, str) and len(value) > schema.get("maxLength", len(value)):
        found.append(f"{where} has more than {schema['maxLength']} characters")
    if isinstance(value, list) and len(value) > schema.get("maxItems", len(value)):
        # Too many items: what each of them breaks would add nothing.
        found.append(f"{where} must hold at most {schema['maxItems']} items")
    elif isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            found += problems(item, schema["items"], f"{where}[{index}]", refs, base, limit)
            if limit is not None and len(found) > limit:
                break
    return found


def _names(value, schema, refs, base):
    """Whether `value` has every constant property that `schema` names, as a `kind`."""
    while "$ref" in schema and refs is not None:
        schema, base = refs(schema["$ref"], base)
    constants = {
        key: sub["const"] for key, sub in schema.get("properties", {}).items() if "const" in sub
    }
    return (
        isinstance(value, dict)
        and bool(constants)
        and all(key in value and _same(value[key], const) for key, const in constants.items())
    )


def _same(value, other):
    """Whether two JSON values are equal, as JSON compares them: a boolean is never a number."""
    if isinstance(value, bool) or isinstance(other, bool):
        return type(value) is type(other) and value == other
    if isinstance(value, dict) and isinstance(other, dict):
        return value.keys() == other.keys() and all(_same(value[k], other[k]) for k in value)
    if isinstance(value, list) and isinstance(other, list):
        return len(value) == len(other) and all(map(_same, value, other))
    if isinstance(value, (dict, list)) or isinstance(other, (dict, list)):
        return False
    return value == other


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
