"""Remove secrets from text derived from agent output before it is printed or stored.

Redaction is a safety net, not a guarantee: it catches the values NexKit can know about
and well-known token shapes. The real protection is that jobs running Claude hold no
write token and that real secrets stay out of the agent's environment.
"""

from __future__ import annotations

import base64
import os
import re
from urllib.parse import quote

MASK = "***"
MIN_LENGTH = 8
SECRET_NAMES = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "GITHUB_TOKEN", "NEXKIT_PUSH_TOKEN")
SECRET_WORDS = ("TOKEN", "SECRET", "KEY", "PASSWORD", "CREDENTIAL")

PATTERNS = (
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.S
        ),
        MASK,
    ),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{8,}"), MASK),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), MASK),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"), MASK),
    (
        re.compile(
            r"(?i)(\bauthorization[\"']?\s*[:=]\s*[\"']?(?:bearer|token|basic)\s+)[^\s\"']+"
        ),
        rf"\g<1>{MASK}",
    ),
    (re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]{8,}"), rf"\g<1>{MASK}"),
    # password=… and pwd=… in connection strings, including DB_PASSWORD=…
    (
        re.compile(r"(?i)((?<![a-z0-9])(?:password|pwd)=)(?:\"[^\"]*\"|'[^']*'|[^\s;&\"',)]+)"),
        rf"\g<1>{MASK}",
    ),
    # scheme://user:password@host
    (re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^\s:/@]*:)[^\s/@]+@"), rf"\g<1>{MASK}@"),
)


def secret_values(environ):
    """Values of credentials and of variables whose names suggest a secret."""
    values = set()
    for name, value in environ.items():
        upper = name.upper()
        if len(value) < MIN_LENGTH:
            continue
        if name in SECRET_NAMES or any(word in upper for word in SECRET_WORDS):
            values.add(value)
            # The same value, encoded the usual ways (echo adds a newline before base64).
            values.add(quote(value, safe=""))
            for raw in (value, value + "\n"):
                values.add(base64.b64encode(raw.encode()).decode().rstrip("="))
    return values


class Redactor:
    """Replaces secrets with ***. Build it from the environment the agent runs in."""

    def __init__(self, environ=None):
        values = secret_values(os.environ if environ is None else environ)
        ordered = sorted(values, key=len, reverse=True)
        self._values = re.compile("|".join(map(re.escape, ordered))) if ordered else None

    def __call__(self, text):
        if not text:
            return text
        if self._values:
            text = self._values.sub(MASK, text)
        for pattern, replacement in PATTERNS:
            text = pattern.sub(replacement, text)
        return text

    def data(self, value):
        """Redact every string in a JSON-like value."""
        if isinstance(value, str):
            return self(value)
        if isinstance(value, list):
            return [self.data(item) for item in value]
        if isinstance(value, dict):
            return {self(key): self.data(item) for key, item in value.items()}
        return value
