"""The largest state document (docs/engine.md#the-state-document), and how what comes in is
kept within it.

A host keeps the document where its work lives. The protocol bounds it, as `encoded` writes
it, so that a host can keep it as base64 in a text field of 65,536 characters. What results
and events bring is bounded as it comes in: each list to the most items `state.schema.json`
allows (MOST), each text to the most characters that is enough for what a person reads
(LONGEST), and a failure signature, which rounds compare, is never cut but named by its hash
when it is long. The history that the prompts show, the fix
rounds' notes, summaries and causes and the last review's findings, is clipped as the prompts
clip it (HISTORY). The texts that a person reads in full where the host keeps them are
shortened further only when a document would still pass the limit (`fit`), the history after
the others, and the lists of failing checks in the fix records last of all. The engine never
answers a larger document, and a host that builds a first one keeps it the same way.
"""

from __future__ import annotations

import hashlib
import json

# The most bytes of a state document as `encoded` writes it. Its base64 takes 64,000
# characters, which leaves room in a text of 65,536 characters for the words around it.
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
    # The fix rounds' records, of which the fix prompt shows the latest five, and the checks
    # that still failed after one.
    "fixes": 5,
    "fixFailing": 30,
}
# The most characters of each text that comes in: long enough for a question, an option or a
# reason that a person must read, and for a check's name, which rounds compare failures by.
LONGEST = {
    "question": 1500,
    "option": 500,
    "title": 256,
    "path": 300,
    "finding": 300,
    "reason": 1000,
    "check": 40,
}
# The most characters of each text of the history, as the prompts clip it, ending a shortened
# text with a line of its own: a fix round's
# note, summary and cause, a finding's body, and the failure signature that a fix record shows.
HISTORY = {"note": 2000, "summary": 4000, "cause": 2000, "body": 1500, "signature": 200}
# The most bytes of a failure signature as JSON writes it; a longer one is named by its hash.
SIGNATURE = 80
# The most checks a diagnosis round looks into, so that its prompt stays focused.
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


def _review(severity):
    def found(state):
        review = state["rounds"].get("lastReview") or {}
        return [
            (item, "body") for item in review.get("findings") or [] if item["severity"] == severity
        ]

    return found


def _review_files(state):
    review = state["rounds"].get("lastReview") or {}
    return [(item, "file") for item in review.get("findings") or []]


def _fix_texts(state):
    """The notes, summaries and causes of the fix records, and the note of the round under
    way, which becomes a record's."""
    rounds = state["rounds"]
    found = [
        (record, key)
        for record in rounds.get("fixes") or []
        for key in ("note", "summary", "cause")
    ]
    return found + ([(rounds["fixRound"], "note")] if rounds.get("fixRound") else [])


def _fix_checks(state):
    """The names and failure signatures that a fix record shows of the checks that still
    failed after it, and the files of the conflicts in a record of a host's first document.
    Only for display: `rounds.signatures` and `rounds.failing` decide."""
    found = []
    for record in state["rounds"].get("fixes") or []:
        for entry in record.get("failing") or []:
            found += [(entry, "name"), (entry, "signature")]
        paths = record.get("conflicts") or []
        found += [(paths, index) for index in range(len(paths))]
    return found


# What is shortened, in this order, while a document does not fit: texts that a person reads
# in full where the host keeps them, never a count, a status, an id, a list's length, a split
# part's title, which names the part it files, or a failure signature, which rounds compare.
# Findings go first, the suggestions before the blocking ones, since a fix round acts on the
# blocking ones. The history that the prompts show comes after all of these: the last
# review's findings, then the fix rounds' texts, then the checks of their records and, last,
# the files of the last review's findings.
SHORTENED = (
    _findings("suggestion"),
    _findings("blocking"),
    _questions,
    _paths,
    _attention,
    _repeated,
    _review("suggestion"),
    _review("blocking"),
    _fix_texts,
    _fix_checks,
    _review_files,
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
    return _forgotten(state, limit)


def _forgotten(state, limit):
    """`state` within `limit` after the oldest fix records forgot which checks still failed
    after them, or None when that does not fit it either. A record without `failing` is one
    whose checks did not run, which a prompt shows as unknown; this is the last resort, for a
    document whose other lists are all as long as the schema allows."""
    for record in state["rounds"].get("fixes") or []:
        if record.pop("failing", None) is not None and size(state) <= limit:
            return state
    return state if size(state) <= limit else None
