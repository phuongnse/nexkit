"""The largest state document (docs/engine.md#the-state-document), and how a document is
shortened to fit it.

A host keeps the document where its work lives, and the GitHub host keeps it as base64 in
one comment, which GitHub cuts at 65,536 characters. The protocol therefore bounds the
document as `dump` writes it. The engine never answers a larger one, and a host that builds a
first document, as an import does, shortens it the same way.
"""

from __future__ import annotations

import json

# The most characters of a state document as `dump` writes it. Its base64 takes 64,000
# characters, which leaves room in a GitHub comment for the words around it.
STATE_LIMIT = 48_000
# The most of each list that a state document keeps of what results and events bring:
# NexKit 6's own limits. With them, shortening texts always fits a document in
# STATE_LIMIT, however long the texts were.
MOST = {"questions": 10, "options": 6, "protectedPaths": 20, "findings": 30, "signatures": 30}
# What ends a text that was shortened.
CUT = "..."


def dump(state):
    """A state document as the protocol measures and stores it: JSON in ASCII, its keys
    sorted and no spaces, so the same document always gives the same text."""
    return json.dumps(state, sort_keys=True, separators=(",", ":"))


def size(state):
    return len(dump(state))


def _findings(severity):
    def found(state):
        return [
            (item, "text") for item in state["rounds"]["findings"] if item["severity"] == severity
        ]

    return found


def _questions(state):
    found = []
    for question in (state["plan"] or {}).get("questions") or []:
        found.append((question, "question"))
        found += [(question["options"], index) for index in range(len(question["options"]))]
    return found


def _titles(state):
    return [(part, "title") for part in (state["plan"] or {}).get("split") or []]


def _paths(state):
    paths = (state["plan"] or {}).get("protectedPaths") or []
    return [(paths, index) for index in range(len(paths))]


def _signatures(state):
    signatures = state["rounds"]["signatures"]
    return [(signatures, index) for index in range(len(signatures))]


def _attention(state):
    return [(state["attention"], "text")] if state["attention"] else []


# What is shortened, in this order, while a document does not fit: texts that a person reads
# in full where the host keeps them, never a count, a status, an id or a list's length, which
# decisions read. Findings go first, the suggestions before the blocking ones, as NexKit 6's
# state comment shortens them first.
SHORTENED = (
    _findings("suggestion"),
    _findings("blocking"),
    _questions,
    _titles,
    _paths,
    _signatures,
    _attention,
)


def _cut(text, most):
    return text if len(text) <= most else text[:most] + CUT


def fit(state, limit=STATE_LIMIT):
    """`state` within `limit`, or None when no shortening fits it, as only lists longer than
    MOST allows can make it. It is unchanged when it fits. Otherwise each group of SHORTENED
    in turn has every text cut to the most characters that fit, after the earlier groups
    were cut to nothing; it changes `state` in place."""
    if size(state) <= limit:
        return state
    for group in SHORTENED:
        found = group(state)
        if not found:
            continue
        texts = [holder[key] for holder, key in found]

        def cut(most, found=found, texts=texts):
            for (holder, key), text in zip(found, texts, strict=True):
                holder[key] = _cut(text, most)
            return size(state) <= limit

        if not cut(0):
            continue
        low, high = 0, max(len(text) for text in texts)
        while low < high:
            middle = (low + high + 1) // 2
            low, high = (middle, high) if cut(middle) else (low, middle - 1)
        cut(low)
        return state
    return None
