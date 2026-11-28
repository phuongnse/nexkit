"""The protocol names keys in camelCase; NexKit's records, as the engine's, use snake_case. These
two converters rename every key of a record, so the host and the engine share them. Standard
library only, as `nexkit.textutil` is."""

from __future__ import annotations

import re


def _camel_key(key):
    head, *rest = key.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


def _snake_key(key):
    return re.sub(r"[A-Z]", lambda match: "_" + match.group().lower(), key)


def _keys(value, rename):
    if isinstance(value, dict):
        return {rename(key): _keys(item, rename) for key, item in value.items()}
    if isinstance(value, list):
        return [_keys(item, rename) for item in value]
    return value


def camel(value):
    """`value` with each key of its records in camelCase, as the protocol sends it."""
    return _keys(value, _camel_key)


def snake(value):
    """`value` with each key of its records in snake_case, as the engine reads it."""
    return _keys(value, _snake_key)
