"""Where models are served. A provider declares its settings and which of them are secrets."""

from __future__ import annotations

from .base import Provider
from .claude_code import CREDENTIALS


class Anthropic(Provider):
    """Claude's own service: a subscription token or an API key, whichever is set."""

    name = "anthropic"
    fields = {}

    def credentials(self, agent):
        return CREDENTIALS
