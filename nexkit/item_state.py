"""Each work item's state document (docs/engine.md#the-state-document) in one comment of
NexKit's App: on the issue, or on a person's pull request for a review-only item.

The comment ends with a marker that holds the document as base64 JSON, as NexKit's other
markers do, and only such a marker of the App's comment, where it ends the comment, counts.
A write is made only while the stored revision is still the one its caller read, so a
caller whose write is refused reads the stored state again and sends its event again.
"""

from __future__ import annotations

import base64
import json
import re

from . import protocol
from .protocol.size import STATE_LIMIT, dump, size
from .state import TEXT_LIMIT, by_bot

PREFIX = "<!-- nexkit:item-state "
# Only the marker that ends the comment.
PATTERN = re.compile(r"<!-- nexkit:item-state ([A-Za-z0-9+/=]+) -->\s*\Z")
# How many times `apply` answers an event again after a write came first.
TRIES = 3
# What a state comment that lost a race to be the first says once it holds no marker.
LOST_TEXT = "### NexKit state\n\nAnother write stored this work item's first state first."


class StateError(ValueError):
    """A state document that cannot be stored or read, such as one that breaks its schema."""


class Moved(Exception):
    """Other writes kept coming first, so the event was not answered on the stored state."""


def render(state):
    """The state comment for `state`, which must fit the schema and GitHub's limit: the
    engine keeps a document within `size.STATE_LIMIT`, whose base64 leaves room for these
    words."""
    found = protocol.check(state, "state.schema.json", "state")
    if found:
        raise StateError("; ".join(found))
    if size(state) > STATE_LIMIT:
        raise StateError(f"The state document passes the limit of {STATE_LIMIT} characters.")
    data = base64.b64encode(dump(state).encode()).decode()
    body = (
        "### NexKit state\n\n"
        f"This work item is `{state['state']}`, at revision {state['revision']}. NexKit keeps "
        f"its state here.\n\n{PREFIX}{data} -->"
    )
    if len(body) > TEXT_LIMIT:
        raise StateError(f"The state comment passes GitHub's limit of {TEXT_LIMIT} characters.")
    return body


def _document(comment):
    match = PATTERN.search(comment.get("body") or "") if by_bot(comment) else None
    if not match:
        return None
    try:
        state = json.loads(base64.b64decode(match.group(1)))
    except ValueError as error:
        raise StateError(f"Comment {comment['id']} holds no state document: {error}") from None
    found = protocol.check(state, "state.schema.json", "state")
    if found:
        raise StateError(f"Comment {comment['id']} holds a broken state document: {found[0]}")
    return state


def find(comments):
    """(comment, state) of the oldest state comment among `comments`, or (None, None). A
    newer one can only be a first write that lost (see `write`)."""
    for comment in comments:
        state = _document(comment)
        if state is not None:
            return comment, state
    return None, None


def read(gh, number):
    """(comment, state) of work item `number`, or (None, None) before its first write."""
    return find(gh.comments(number))


def write(gh, number, state, revision):
    """Store `state` as work item `number`'s, when the stored revision is still `revision`,
    the one its caller read (None: no state was stored). Answer whether it was stored; a
    write that is not leaves the stored state as it is.

    GitHub has no compare-and-set, so the revision is read again just before the edit. A
    write by someone else between that read and the edit is overwritten: the check narrows
    the race to that moment and does not close it. Two first writes that race both post a
    comment; the older one is the state, and the newer one loses its marker."""
    body = render(state)
    comment, stored = read(gh, number)
    if (stored or {}).get("revision") != revision:
        return False
    if comment is not None:
        gh.update_comment(comment["id"], body)
        return True
    posted = gh.comment(number, body)
    first, _ = read(gh, number)
    if first is not None and first["id"] != posted["id"]:
        gh.update_comment(posted["id"], LOST_TEXT)
        return False
    return True


def apply(gh, number, answer, tries=TRIES):
    """Answer one event on work item `number`'s stored state and store the answer: read the
    state, call `answer(state)`, which gives (state, actions, changed) as the engine's `step`
    does, and write the new state on the revision read. When another write came first, read
    the stored state again and answer again, at most `tries` times. Gives (state, actions) of
    the answer that was stored, or of one that changed nothing, which is not written."""
    for _ in range(tries):
        _, state = read(gh, number)
        new, actions, changed = answer(state)
        if not changed:
            return state, []
        if write(gh, number, new, state and state["revision"]):
            return new, actions
    raise Moved(f"Other writes to the state of #{number} came first {tries} times.")
