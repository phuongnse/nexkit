"""The largest state document (docs/engine.md#the-state-document), and how what comes in is
kept within it.

A host keeps the document where its work lives, and the GitHub host keeps it as base64 in
one comment, which GitHub refuses past 65,536 characters. The protocol therefore bounds the
document as `encoded` writes it. What results and events bring is bounded as it comes in:
each list to the most items `state.schema.json` allows (MOST), each text to the most
characters NexKit 6 keeps of it (LONGEST), and a failure signature, which rounds compare, is
never cut but named by its hash when it is long. The texts that a person reads in full where
the host keeps them are shortened further only when a document would still pass the limit
(`fit`). The engine never answers a larger document, and a host that builds a first one, as
an import does, keeps it the same way.
"""

from __future__ import annotations

import hashlib
import json

# The most bytes of a state document as `encoded` writes it. Its base64 takes 64,000
# characters, which leaves room in a GitHub comment for the words around it.
STATE_LIMIT = 48_000
# The most of each list that a state document keeps, as `maxItems` in `state.schema.json`.
# A split of more parts keeps none of them, and its count says how many it named.
MOST = {
    "questions": 10,
    "options": 6,
    "protectedPaths": 20,
    "split": 10,
    "findings": 30,
    "signatures": 30,
    # The failing checks of the latest checks, each by a short digest: 15 checks of 20 shards
    # each fail at most 300 ways at once.
    "failing": 300,
    "repeated": 150,
    "criteria": 50,
    "checklist": 50,
}
# The most characters of each text that comes in: NexKit 6's own (`state.questions`,
# `state.plan_data_record`), for a reason a person reads, enough to say it, and for a check's
# name, which rounds compare failures by, as long as NexKit's own may be (`config.NAME`).
LONGEST = {
    "question": 1500,
    "option": 500,
    "title": 256,
    "path": 300,
    "finding": 300,
    "reason": 1000,
    "check": 40,
}
# The most bytes of a failure signature as JSON writes it; a longer one is named by its hash.
SIGNATURE = 80
# The most checks a diagnosis round looks into (NexKit 6's `state.MAX_QUESTIONS`).
DIAGNOSES = 10
# The hex digits of a failing check's digest (`failure_key`).
KEY = 12
# What ends a text that was shortened.
CUT = "..."


def dump(state):
    """A state document as the protocol stores it: JSON with its keys sorted and no spaces,
    so the same document always gives the same text."""
    return json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def encoded(state):
    """`dump` in UTF-8, which keeps a text that JSON can hold but UTF-8 cannot, such as half
    of a surrogate pair, as it is."""
    return dump(state).encode("utf-8", "surrogatepass")


def decoded(data):
    return json.loads(data.decode("utf-8", "surrogatepass"))


def size(state):
    return len(encoded(state))


def clip(text, kind):
    """`text` with at most LONGEST[kind] characters."""
    text = str(text)
    most = LONGEST[kind]
    return text if len(text) <= most else text[:most] + CUT


def signature(text):
    """A failure signature as the state keeps it: itself, or its hash when it is long, so
    that two signatures compare as they did and none is cut into another."""
    text = str(text)
    if len(json.dumps(text, ensure_ascii=False).encode("utf-8", "surrogatepass")) <= SIGNATURE:
        return text
    found = hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()
    return f"sha256:{found}"


def failure_key(name, text):
    """A failing check as rounds compare it: a short digest of its name and its failure
    signature, the same for the same failure of the same check, so that the checks before a
    fix round keep little room each however many failed."""
    found = f"{name}\n{text}".encode("utf-8", "surrogatepass")
    return hashlib.sha256(found).hexdigest()[:KEY]


def _findings(severity):
    def found(state):
        return [
            (item, "text") for item in state["rounds"]["findings"] if item["severity"] == severity
        ]

    return found


def _questions(state):
    found = []
    asked = (state["plan"] or {}).get("questions") or []
    asked = [*asked, *(state.get("questions") or {}).get("items", [])]
    for question in asked:
        found.append((question, "question"))
        found += [(question["options"], index) for index in range(len(question["options"]))]
    return found


def _paths(state):
    paths = (state["plan"] or {}).get("protectedPaths") or []
    return [(paths, index) for index in range(len(paths))]


def _attention(state):
    return [(state["attention"], "text")] if state["attention"] else []


def _repeated(state):
    """The names of repeated checks past those a diagnosis round looks into, which only the
    host keeps for people; NexKit's own names never need it, being short."""
    names = state["rounds"].get("repeated") or []
    return [(names, index) for index in range(DIAGNOSES, len(names))]


# What is shortened, in this order, while a document does not fit: texts that a person reads
# in full where the host keeps them, never a count, a status, an id, a list's length, a split
# part's title, which names the part it files, or a failure signature, which rounds compare.
# Findings go first, the suggestions before the blocking ones, as NexKit 6's state comment
# shortens them first.
SHORTENED = (
    _findings("suggestion"),
    _findings("blocking"),
    _questions,
    _paths,
    _attention,
    _repeated,
)


def _cut(text, most):
    return text if len(text) <= most else text[:most] + CUT


def fit(state, limit=STATE_LIMIT):
    """`state` within `limit`, or None when no shortening fits it. It is unchanged when it
    fits. Otherwise each group of SHORTENED in turn has every text cut to the most characters
    that fit, after the earlier groups were cut to nothing; it changes `state` in place."""
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
