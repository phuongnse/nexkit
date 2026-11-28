"""Each work item's state document (docs/engine.md#the-state-document) in one comment of
NexKit's App: on the issue, or on a person's pull request for a review-only item.

The comment ends with a marker that holds the document as base64 JSON, as NexKit's other
markers do, and only such a marker of the App's comment, where it ends the comment, counts.
A write is made only while the stored revision is still the one its caller read, and only
of the next revision, so a caller whose write is refused reads the stored state again and
sends its event again. A line before the state marker, the cursor (`nexkit:item-since`),
names the newest source (comment, review or label event) that the host took from GitHub,
by its time and its event id, so that the next run answers only what came after it. It
counts only on that line, in the comment that the state marker counts in, and only the
host's own `apply` moves it: another writer's write keeps it. A caller whose every try is
refused leaves its event at its source, past the cursor, for the next run to take.

Every writer of one item's state runs in one concurrency group keyed by the work item, an
issue's commands and its pull request's rounds alike, so the check below only catches what
that misses. A person who may write to the repository can edit the App's comment, and it
still has the App as its author: a state comment that someone else changed counts for nothing,
and `find` raises `Edited` for it, so that nothing posts a second state comment over it and
the item waits for a person. Every other marker counts only in a comment that
`state.bot_record` accepts, as docs/security.md#comments-and-markers says.
"""

from __future__ import annotations

import base64
import random
import re
import time

from . import protocol
from .protocol.size import STATE_LIMIT, decoded, encoded, size
from .state import TEXT_LIMIT, bot_record, by_bot, changed_by

PREFIX = "<!-- nexkit:item-state "
# Only the marker that ends the comment.
PATTERN = re.compile(r"<!-- nexkit:item-state ([A-Za-z0-9+/=]+) -->\s*\Z")
# The cursor, only on the line before that marker: the time and the event id of the newest
# source taken.
SINCE_PREFIX = "<!-- nexkit:item-since "
SINCE_PATTERN = re.compile(
    r"<!-- nexkit:item-since (\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) ([a-z]+(?:-[a-z]+)*)-([0-9]+) -->\n"
    r"<!-- nexkit:item-state [A-Za-z0-9+/=]+ -->\s*\Z"
)
# The kinds of source that name an event, in the order that sources of one second take.
SOURCES = ("comment", "review", "issue-event", "dispatch", "run")
# How many times `apply` answers an event after a write came first, and how many seconds it
# waits before each try again, each wait from half to one and a half times as long, so that
# two writers that met do not meet again.
TRIES = 4
BACKOFF = (1, 2, 4)
# What a state comment that lost a race to be the first says once it holds no marker.
LOST_TEXT = "### NexKit state\n\nAnother write stored this work item's first state first."


class StateError(ValueError):
    """A state document that cannot be stored or read, such as one that breaks its schema."""


class Edited(Exception):
    """The App's state comment, `.comment`, was changed by someone else. It counts for nothing
    and is not absent: a writer that took it for absent would post a second state comment."""

    def __init__(self, comment):
        super().__init__(f"The state comment {comment['id']} was changed by someone else.")
        self.comment = comment


class Moved(Exception):
    """Other writes kept coming first, so the event was not answered on the stored state."""


def order(since):
    """The place of the cursor `since`, (at, event id), among the sources: its time, then
    its kind in `SOURCES`, then its number."""
    kind, _, number = since[1].rpartition("-")
    return since[0], SOURCES.index(kind), int(number)


def render(state, since=None):
    """The state comment for `state`, which must fit the schema and GitHub's limit: the
    engine keeps a document within `size.STATE_LIMIT`, whose base64 leaves room for these
    words. `since`, the cursor (at, event id), is the line before the state marker."""
    found = protocol.check(state, "state.schema.json", "state")
    if found:
        raise StateError("; ".join(found))
    if size(state) > STATE_LIMIT:
        raise StateError(f"The state document passes the limit of {STATE_LIMIT} characters.")
    data = base64.b64encode(encoded(state)).decode()
    cursor = f"{SINCE_PREFIX}{since[0]} {since[1]} -->\n" if since else ""
    body = (
        "### NexKit state\n\n"
        f"This work item is `{state['state']}`, at revision {state['revision']}. NexKit keeps "
        f"its state here.\n\n{cursor}{PREFIX}{data} -->"
    )
    if since and not SINCE_PATTERN.search(body):
        raise StateError(f"A cursor of {since!r} names no source.")
    if len(body) > TEXT_LIMIT:
        raise StateError(f"The state comment passes GitHub's limit of {TEXT_LIMIT} characters.")
    return body


def _document(comment):
    match = PATTERN.search(comment.get("body") or "") if bot_record(comment) else None
    if not match:
        return None
    try:
        state = decoded(base64.b64decode(match.group(1)))
    except ValueError as error:
        raise StateError(f"Comment {comment['id']} holds no state document: {error}") from None
    state = protocol.upgrade(state)
    found = protocol.check(state, "state.schema.json", "state")
    if found:
        raise StateError(f"Comment {comment['id']} holds a broken state document: {found[0]}")
    return state


def since(comment):
    """(at, event id) of the cursor in the state comment `comment`, the newest source that a
    run took, or None when it holds none or does not count as the App's record."""
    match = SINCE_PATTERN.search(comment.get("body") or "") if bot_record(comment) else None
    if not match or match.group(2) not in SOURCES:
        return None
    return match.group(1), f"{match.group(2)}-{match.group(3)}"


_stored_since = since


def _edited(comment):
    """Whether `comment` is the App's, was changed by someone else and still looks like a
    state comment: its heading or its marker is left."""
    body = comment.get("body") or ""
    return (
        by_bot(comment)
        and bool(changed_by(comment))
        and (body.startswith("### NexKit state") or PREFIX in body)
    )


def find(comments):
    """(comment, state) of the oldest state comment among `comments`, or (None, None). A
    newer one can only be a first write that lost (see `write`). Raises `Edited` for a
    state comment that someone other than the App changed."""
    for comment in comments:
        if _edited(comment):
            raise Edited(comment)
        state = _document(comment)
        if state is not None:
            return comment, state
    return None, None


def read(gh, number):
    """(comment, state) of work item `number`, or (None, None) before its first write. Raises
    `Edited` when someone other than the App changed its state comment."""
    return find(gh.comments(number))


def write(gh, number, state, read, since=None):
    """Store `state` as work item `number`'s, when what its caller `read`, the (comment,
    state) of `read`, is still stored, and `state` is its next revision: one more than the
    revision read, or at least 1 for a first write. Answer whether it was stored; a write that
    is not leaves the stored state as it is. The cursor stored with it is `since`, or, with
    none, the cursor of the comment that was read, so that a writer that takes no source
    never moves it.

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
    if since is None and comment is not None:
        since = _stored_since(comment)
    body = render(state, since)
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


def advance(gh, number, read, since):
    """Move the cursor of work item `number`'s state comment to `since`, keeping the
    document and its revision, when what its caller `read` is still stored, checked as `write`
    checks it by reading the comment again by its id. Answer whether it moved; one that did
    not leaves the comment as it is."""
    comment, stored = read
    if comment is None or _document(gh.issue_comment(comment["id"])) != stored:
        return False
    gh.update_comment(comment["id"], render(stored, since))
    return True


def apply(gh, number, answer, tries=TRIES, sleep=time.sleep, jitter=random.random, since=None):
    """Answer one event on work item `number`'s stored state and store the answer: read the
    state, call `answer(state)`, which gives (state, actions, changed) as the engine's `step`
    does, and write the new state on what was read. When another write came first, wait a
    little, read the stored state again and answer again, at most `tries` times in all. Gives
    (state, actions) of the answer that was stored, or of one that changed nothing, which is
    not written. Raises `Moved` when every try lost: the caller sends the event again later,
    as a failed run is started again, and never drops it.

    `since`, (at, event id), is the source that the event was taken from, which the stored
    cursor moves to when it is past it, together with the answer, or alone, on the same
    revision, when the answer changed nothing: an old remark is not answered later on a state
    that moved on. Without it, or when the cursor is already there, the cursor stays."""
    for attempt in range(tries):
        if attempt:
            wait = BACKOFF[min(attempt, len(BACKOFF)) - 1]
            sleep(wait * (0.5 + jitter()))
        found = read(gh, number)
        new, actions, changed = answer(found[1])
        stored = _stored_since(found[0]) if found[0] else None
        moves = since if since and (not stored or order(since) > order(stored)) else None
        if not changed:
            if moves is None or found[0] is None or advance(gh, number, found, moves):
                return found[1], []
        elif write(gh, number, new, found, moves):
            return new, actions
    raise Moved(f"Other writes to the state of #{number} came first {tries} times.")
