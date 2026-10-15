"""Check an agent's structured result against its stage schema.

Agent output is untrusted, and runtimes enforce schemas differently, so NexKit checks the
result itself before anything uses it. Only the parts of JSON Schema that NexKit's stage
schemas use are supported: `type`, `enum`, `properties`, `required` and `items`. Keys that
a schema does not name are left as they are.
"""

from __future__ import annotations

TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "integer": int,
    "number": (int, float),
}


def problems(value, schema, where="result"):
    """The ways `value` breaks `schema`, as short sentences. Empty when it fits."""
    found = []
    kind = schema.get("type")
    if kind:
        expected = TYPES[kind]
        is_bool = isinstance(value, bool)
        if not isinstance(value, expected) or (is_bool and kind in ("integer", "number")):
            return [f"{where} must be {'an' if kind[0] in 'aeiou' else 'a'} {kind}"]
    if "enum" in schema and value not in schema["enum"]:
        allowed = ", ".join(repr(item) for item in schema["enum"])
        found.append(f"{where} must be one of {allowed}")
    if isinstance(value, dict):
        for key in schema.get("required", ()):
            if key not in value:
                found.append(f"{where}.{key} is missing")
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                found += problems(value[key], sub, f"{where}.{key}")
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            found += problems(item, schema["items"], f"{where}[{index}]")
    return found
