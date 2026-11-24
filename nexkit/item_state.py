"""Each work item's state document (docs/engine.md#the-state-document) in one comment of
NexKit's App: on the issue, or on a person's pull request for a review-only item.

The comment ends with a marker that holds the document as base64 JSON, as NexKit's other
markers do, and only such a marker of the App's comment, where it ends the comment, counts.
A write is made only while the stored revision is still the one its caller read, and only
of the next revision, so a caller whose write is refused reads the stored state again and
sends its event again.

Every writer of one item's state runs in one concurrency group keyed by the work item, an
issue's commands and its pull request's rounds alike, so the check below only catches what
that misses. A person who may write to the repository can edit the App's comment, and it
still reads as the App's: phuongnse/nexkit#329 closes that for every marker of NexKit.
"""

from __future__ import annotations

import base64
import random
import re
import time

from . import protocol
from .protocol.size import STATE_LIMIT, decoded, encoded, size
from .state import TEXT_LIMIT, by_bot

PREFIX = "<!-- nexkit:item-state "
# Only the marker that ends the comment.
PATTERN = re.compile(r"<!-- nexkit:item-state ([A-Za-z0-9+/=]+) -->\s*\Z")
# How many times `apply` answers an event after a write came first, and how many seconds it
# waits before each try again, each wait from half to one and a half times as long, so that
# two writers that met do not meet again.
TRIES = 4
BACKOFF = (1, 2, 4)
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
    data = base64.b64encode(encoded(state)).decode()
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
        state = decoded(base64.b64decode(match.group(1)))
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


def write(gh, number, state, read):
    """Store `state` as work item `number`'s, when what its caller `read`, the (comment,
    state) of `read`, is still stored, and `state` is its next revision: one more than the
    revision read, or at least 1 for a first write. Answer whether it was stored; a write that
    is not leaves the stored state as it is.

    GitHub has no compare-and-set, so the comment is read again by its id just before the
    edit. A write by someone else between that read and the edit is overwritten: the check
    narrows the race to that moment and does not close it. Two first writes that race both
    post a comment; the older one is the state, and the newer one loses its marker. GitHub's
    list of comments can lag behind a comment just posted, so both may then think they won;
    the one concurrency group of the item's writers is what keeps that from happening."""
    comment, stored = read
    expected = stored["revision"] + 1 if stored else max(state["revision"], 1)
    if state["revision"] != expected or (stored and stored["item"] != state["item"]):
        return False
    body = render(state)
    if comment is not None:
        if _document(gh.issue_comment(comment["id"])) != stored:
            return False
        gh.update_comment(comment["id"], body)
        return True
    if find(gh.comments(number))[0] is not None:
        return False
    posted = gh.comment(number, body)
    first, _ = find(gh.comments(number))
    if first is not None and first["id"] != posted["id"]:
        gh.update_comment(posted["id"], LOST_TEXT)
        return False
    return True


def apply(gh, number, answer, tries=TRIES, sleep=time.sleep, jitter=random.random):
    """Answer one event on work item `number`'s stored state and store the answer: read the
    state, call `answer(state)`, which gives (state, actions, changed) as the engine's `step`
    does, and write the new state on what was read. When another write came first, wait a
    little, read the stored state again and answer again, at most `tries` times in all. Gives
    (state, actions) of the answer that was stored, or of one that changed nothing, which is
    not written. Raises `Moved` when every try lost: the caller sends the event again later,
    as a failed run is started again, and never drops it."""
    for attempt in range(tries):
        if attempt:
            wait = BACKOFF[min(attempt, len(BACKOFF)) - 1]
            sleep(wait * (0.5 + jitter()))
        found = read(gh, number)
        new, actions, changed = answer(found[1])
        if not changed:
            return found[1], []
        if write(gh, number, new, found):
            return new, actions
    raise Moved(f"Other writes to the state of #{number} came first {tries} times.")
