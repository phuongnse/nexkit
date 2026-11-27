"""Each work item's state document in a comment of NexKit's App
(docs/engine.md#the-state-document): written only as the next revision of the one read, and
never past GitHub's comment size."""

import base64
import copy
import json
import re
import unittest
from pathlib import Path

from nexkit import engine, item_state, protocol
from nexkit.engine.machine import blank
from nexkit.protocol.size import (
    DIAGNOSES,
    HISTORY,
    KEY,
    LONGEST,
    MOST,
    STATE_LIMIT,
    dump,
    encoded,
    fit,
    size,
)
from nexkit.state import TEXT_LIMIT
from tests.support import BOT_LOGIN, FakeGitHub

ROOT = Path(__file__).resolve().parent.parent
PERSON = {"id": "octocat", "role": "decides"}
NOW = "2026-11-23T15:01:11Z"


def event(kind, number, **fields):
    return {"protocol": 1, "kind": kind, "id": f"e{number}", "item": "42", "at": NOW, **fields}


CREATED = event(
    "created",
    1,
    itemKind="change",
    title="A work item",
    text={"name": "body", "ref": "42"},
    actors={"plan": "engine", "change": "engine", "review": "engine"},
    plan=None,
    change=None,
)
PLAN = event("requested", 2, what="plan", by=PERSON, note="", plan=None)
HELD = event("held", 3, label="on-hold")
RELEASED = event("released", 4, label="on-hold")


def step(state, event):
    """The engine's answer, as (state, actions, changed)."""
    answer = engine.answer(
        {"protocol": 1, "call": "step", "state": state, "event": event, "policies": {}}
    )
    assert "error" not in answer, answer
    return answer["state"], answer["actions"], answer["changed"]


def created(gh):
    state, _, _ = step(None, CREATED)
    assert item_state.write(gh, 42, state, (None, None))
    return state


class Sleeps:
    def __init__(self):
        self.waits = []

    def __call__(self, seconds):
        self.waits.append(seconds)


class StoreTest(unittest.TestCase):
    def test_a_written_state_reads_back(self):
        gh = FakeGitHub()
        self.assertEqual(item_state.read(gh, 42), (None, None))
        state = created(gh)
        comment, stored = item_state.read(gh, 42)
        self.assertEqual(stored, state)
        self.assertTrue(comment["body"].startswith("### NexKit state"))
        self.assertIn("`new`, at revision 1", comment["body"])

    def test_a_state_comment_that_someone_else_changed_raises_and_one_the_app_changed_reads(self):
        gh = FakeGitHub()
        state = created(gh)
        [comment] = gh.comments(42)
        comment["editors"] = [BOT_LOGIN, "mallory"]
        with self.assertRaises(item_state.Edited) as raised:
            item_state.read(gh, 42)
        self.assertEqual(raised.exception.comment["id"], comment["id"])
        # Also when its marker is gone and only the heading is left.
        comment["body"] = "### NexKit state\n\nNothing is left."
        with self.assertRaises(item_state.Edited):
            item_state.read(gh, 42)
        # Or the marker is left and the heading is gone.
        comment["body"] = f"Mine now.\n\n{item_state.PREFIX}AAAA -->"
        with self.assertRaises(item_state.Edited):
            item_state.read(gh, 42)
        # A comment of the App that is no state comment counts as absent, as before.
        comment["body"] = "Another record of the App."
        self.assertEqual(item_state.read(gh, 42), (None, None))
        # A comment that only the App changed reads as before.
        comment["editors"] = [BOT_LOGIN]
        self.assertEqual(item_state.read(gh, 42), (None, None))
        created_again = FakeGitHub()
        created(created_again)
        [again] = created_again.comments(42)
        again["editors"] = [BOT_LOGIN]
        self.assertEqual(item_state.read(created_again, 42)[1], state)

    def test_a_state_comment_of_a_person_who_edited_it_is_not_one_of_the_apps(self):
        gh = FakeGitHub()
        created(gh)
        [comment] = gh.comments(42)
        comment["user"] = {"login": "mallory", "type": "User"}
        comment["editors"] = ["mallory"]
        self.assertEqual(item_state.read(gh, 42), (None, None))

    def test_a_write_on_a_revision_that_moved_is_not_made(self):
        gh = FakeGitHub()
        created(gh)
        read = item_state.read(gh, 42)
        held, _, _ = step(read[1], HELD)
        self.assertTrue(item_state.write(gh, 42, held, read))
        # A second answer to the state of revision 1, which is no longer stored.
        late, _, _ = step(read[1], PLAN)
        self.assertFalse(item_state.write(gh, 42, late, read))
        self.assertEqual(item_state.read(gh, 42)[1], held)
        # And a first write when a state is already stored.
        self.assertFalse(item_state.write(gh, 42, read[1], (None, None)))
        self.assertEqual(item_state.read(gh, 42)[1], held)

    def test_only_the_next_revision_is_written(self):
        gh = FakeGitHub()
        state, _, _ = step(None, CREATED)
        self.assertFalse(item_state.write(gh, 42, {**state, "revision": 0}, (None, None)))
        created(gh)
        read = item_state.read(gh, 42)
        # The same revision again, as a write that read the state before the stored one
        # was written, and one that skips a revision.
        for revision in (1, 3):
            self.assertFalse(item_state.write(gh, 42, {**read[1], "revision": revision}, read))
        held, _, _ = step(read[1], HELD)
        self.assertTrue(item_state.write(gh, 42, held, read))

    def test_the_stored_revision_is_read_again_by_the_comment_s_id(self):
        gh = FakeGitHub()
        created(gh)
        read = item_state.read(gh, 42)
        held, _, _ = step(read[1], HELD)

        def listed(number):
            raise AssertionError("a write of a stored state lists no comments")

        gh.comments = listed
        self.assertTrue(item_state.write(gh, 42, held, read))

    def test_an_event_whose_write_lost_is_sent_again_on_the_stored_state(self):
        gh = FakeGitHub()
        created(gh)
        seen, sleeps = [], Sleeps()

        def answer(state):
            seen.append(state)
            if len(seen) == 1:
                # Another job stores its answer to a hold after this one read the state.
                other, _, _ = step(state, HELD)
                self.assertTrue(item_state.write(gh, 42, other, item_state.read(gh, 42)))
            return step(state, PLAN)

        state, actions = item_state.apply(gh, 42, answer, sleep=sleeps, jitter=lambda: 0.5)
        self.assertEqual([found["revision"] for found in seen], [1, 2])
        self.assertTrue(seen[1]["hold"])
        self.assertEqual((state["state"], state["hold"], state["revision"]), ("planning", True, 3))
        self.assertEqual([action["kind"] for action in actions], ["runAgent"])
        self.assertEqual(item_state.read(gh, 42)[1], state)
        self.assertEqual(len(gh.comments(42)), 1)
        self.assertEqual(sleeps.waits, [1])

    def test_writes_that_keep_coming_first_stop_the_event_for_the_caller_to_send_again(self):
        gh = FakeGitHub()
        created(gh)
        sleeps = Sleeps()

        def answer(state):
            # Another job's answer to an event of its own each time.
            other = {**(RELEASED if state["hold"] else HELD), "id": f"e{state['revision']}0"}
            other, _, _ = step(state, other)
            item_state.write(gh, 42, other, item_state.read(gh, 42))
            return step(state, PLAN)

        with self.assertRaises(item_state.Moved):
            item_state.apply(gh, 42, answer, sleep=sleeps, jitter=lambda: 1.0)
        self.assertEqual(item_state.read(gh, 42)[1]["state"], "new")
        # Each wait longer than the one before, by a random part.
        self.assertEqual(sleeps.waits, [1.5, 3.0, 6.0])

    def test_the_cursor_moves_only_with_a_caller_that_takes_a_source(self):
        gh = FakeGitHub()
        created(gh)
        self.assertIsNone(item_state.since(item_state.read(gh, 42)[0]))
        first = ("2026-11-23T15:00:00Z", "comment-7")
        item_state.apply(gh, 42, lambda state: step(state, PLAN), since=first)
        read = item_state.read(gh, 42)
        self.assertEqual((item_state.since(read[0]), read[1]["revision"]), (first, 2))
        # Another writer's write keeps it.
        held, _, _ = step(read[1], HELD)
        self.assertTrue(item_state.write(gh, 42, held, read))
        self.assertEqual(item_state.since(item_state.read(gh, 42)[0]), first)
        # A source that is not past it leaves it where it is, and a later one moves it.
        older = ("2026-11-23T14:00:00Z", "comment-9")
        later = ("2026-11-23T15:00:00Z", "review-1")
        for since, found in ((older, first), (first, first), (later, later)):
            item_state.apply(gh, 42, lambda state: step(state, RELEASED), since=since)
            self.assertEqual(item_state.since(item_state.read(gh, 42)[0]), found)
            item_state.apply(gh, 42, lambda state: step(state, HELD), since=since)

    def test_an_answer_that_changed_nothing_still_moves_the_cursor_on_the_same_revision(self):
        gh = FakeGitHub()
        state = created(gh)
        tick = event("tick", 9, wait="none")
        since = ("2026-11-23T15:00:00Z", "issue-event-3")
        found, actions = item_state.apply(gh, 42, lambda state: step(state, tick), since=since)
        self.assertEqual((found, actions), (state, []))
        read = item_state.read(gh, 42)
        self.assertEqual((read[1], item_state.since(read[0])), (state, since))
        # Nothing to write the second time.
        before = copy.deepcopy(gh.issue_comments[42])
        item_state.apply(gh, 42, lambda state: step(state, tick), since=since)
        self.assertEqual(gh.issue_comments[42], before)

    def test_a_cursor_that_loses_to_another_write_is_set_again_on_the_stored_state(self):
        gh = FakeGitHub()
        created(gh)
        tick = event("tick", 9, wait="none")
        since = ("2026-11-23T15:00:00Z", "comment-3")
        seen = []

        def answer(state):
            seen.append(state["revision"])
            if len(seen) == 1:
                other, _, _ = step(state, HELD)
                self.assertTrue(item_state.write(gh, 42, other, item_state.read(gh, 42)))
            return step(state, tick)

        item_state.apply(gh, 42, answer, since=since, sleep=Sleeps())
        read = item_state.read(gh, 42)
        self.assertEqual((seen, read[1]["revision"], read[1]["hold"]), ([1, 2], 2, True))
        self.assertEqual(item_state.since(read[0]), since)

    def test_a_cursor_that_names_no_source_is_not_written(self):
        state, _, _ = step(None, CREATED)
        for since in (("yesterday", "comment-1"), ("2026-11-23T15:00:00Z", "comment 1")):
            with self.assertRaises(item_state.StateError):
                item_state.render(state, since)

    def test_an_answer_that_changes_nothing_writes_nothing(self):
        gh = FakeGitHub()
        state = created(gh)
        before = copy.deepcopy(gh.issue_comments[42])
        tick = event("tick", 9, wait="none")
        found, actions = item_state.apply(gh, 42, lambda state: step(state, tick))
        self.assertEqual((found, actions), (state, []))
        self.assertEqual(gh.issue_comments[42], before)

    def test_of_two_first_writes_the_older_comment_is_the_state(self):
        gh = FakeGitHub()
        state, _, _ = step(None, CREATED)
        other = copy.deepcopy(state)
        other["hold"] = True
        posted = gh.comment

        def racing(number, body):
            # The other first write posts between this one's read and its post.
            gh.comment = posted
            posted(number, item_state.render(other))
            return posted(number, body)

        gh.comment = racing
        self.assertFalse(item_state.write(gh, 42, state, (None, None)))
        self.assertEqual(item_state.read(gh, 42)[1], other)
        self.assertEqual(gh.comments(42)[1]["body"], item_state.LOST_TEXT)

    def test_only_a_marker_of_the_app_where_it_ends_the_comment_counts(self):
        gh = FakeGitHub()
        body = item_state.render({**blank("42"), "revision": 1})
        gh.human_comment(42, body)
        gh.comment(42, f"{body}\n\nA quote after the marker.")
        self.assertEqual(item_state.read(gh, 42), (None, None))

    def test_a_marker_that_holds_no_state_document_is_an_error(self):
        gh = FakeGitHub()
        gh.comment(42, f"{item_state.PREFIX}e30= -->")
        with self.assertRaises(item_state.StateError):
            item_state.read(gh, 42)


def most(path):
    """The `maxItems` of a list of the state, by its path in `state.schema.json`."""
    schema = protocol.load("state.schema.json")
    for part in path.split("."):
        # `[]` names a list's items.
        schema = schema["items"] if part == "[]" else schema["properties"][part]
        schema = schema["anyOf"][0] if "anyOf" in schema else schema
    return schema["maxItems"]


# A text in the characters that take the most bytes: four in UTF-8, or two once JSON writes
# a quote or a backslash.
WORST = '\U0001f600"\\'


def worst(characters):
    return (WORST * characters)[:characters]


def clipped(characters):
    """A text of the history as `engine.textutil.clip` keeps it at most: the limit, and the
    line that ends a shortened text."""
    return worst(characters) + "\n[truncated]"


def history():
    """The most that the state keeps of the history that the prompts show: five fix records
    with every text, 30 failing checks and 30 conflicting files, and a last review of 30
    findings with the longest bodies."""
    fixes = [
        {
            "round": 20 + n,
            "auto": bool(n % 2),
            "diagnosis": bool(n % 2),
            "note": clipped(HISTORY["note"]),
            "summary": clipped(HISTORY["summary"]),
            "cause": clipped(HISTORY["cause"]),
            "failing": [
                {
                    "name": worst(LONGEST["check"]) + "...",
                    "signature": clipped(HISTORY["signature"]),
                }
                for _ in range(most("rounds.fixes.[].failing"))
            ],
            "conflicts": [worst(LONGEST["path"])] * most("rounds.fixes.[].conflicts"),
        }
        for n in range(most("rounds.fixes"))
    ]
    review = {
        "round": 25,
        "version": "f" * 40,
        "verdict": "request_changes",
        "findings": [
            {
                "severity": "blocking" if n % 2 else "suggestion",
                "file": worst(LONGEST["path"]),
                "line": 9999,
                "body": clipped(HISTORY["body"]),
            }
            for n in range(most("rounds.lastReview.findings"))
        ],
        "leftOut": 12,
    }
    return fixes, review


def largest(with_history=True):
    """The largest state the schema allows: every list as long as its `maxItems`, each text
    as long as the engine keeps it, in the characters that take the most bytes, and in the
    outbox the actions that hold texts, whole. Without the history of the fix rounds and the
    last review, which is shortened after everything else, the other texts keep their room."""
    state = blank("4" * 20)
    state.update(
        revision=99,
        state="awaitingApproval",
        change={"id": "9" * 20, "version": "f" * 40},
        attention={"reason": "question", "text": worst(LONGEST["reason"]), "ref": "9" * 20},
        hold=True,
        observed={
            "changeId": "9" * 20,
            "version": "e" * 40,
            "conflicts": True,
            "merged": False,
            "empty": False,
        },
    )
    # A round that runs keeps every policy it started with.
    state["policies"] = {
        "planApproval": "autoWhenClean",
        "reviewAfterFailingChecks": "onlyAfterPassing",
        "accept": "autoWhenPassed",
        "fixRounds": 10,
        "whenRoundsRunOut": "person",
        "conflicts": "resolve",
        "conflictRounds": 10,
        "splits": True,
        "answers": "recommended",
        "splitParts": 10,
        "splitDepth": 10,
    }
    state.update(title=worst(LONGEST["title"]), depth=9)
    state["waits"] = [
        {"id": f"w99.{n}", "for": "blocker", "until": None, "on": str(4000 + n)}
        for n in range(most("waits"))
    ]
    questions = most("plan.questions")
    state["plan"] = {
        "text": {"name": "plan", "ref": "9" * 20},
        "questions": [
            {
                "question": worst(LONGEST["question"]),
                "options": [worst(LONGEST["option"])] * MOST["options"],
                "recommended": 1,
                "closes": MOST["options"],
            }
            for _ in range(questions)
        ],
        "criteria": 50,
        "tooLarge": True,
        "parts": 10,
        "noChange": False,
        "protectedPaths": [worst(LONGEST["path"])] * most("plan.protectedPaths"),
        "approved": True,
        "split": [
            {"key": n, "title": worst(LONGEST["title"]), "needs": list(range(1, n))}
            for n in range(1, most("plan.split") + 1)
        ],
    }
    # The questions of the round that asked, beside the plan's.
    state["questions"] = {
        "ref": "9" * 20,
        "stage": "implement",
        "items": [
            {
                "question": worst(LONGEST["question"]),
                "options": [worst(LONGEST["option"])] * MOST["options"],
                "recommended": 1,
                "closes": MOST["options"],
                "replans": list(range(1, MOST["options"])),
            }
            for _ in range(most("questions.items"))
        ],
        "noChange": True,
    }
    rounds = state["rounds"]
    rounds.update(started=13, counted=10, conflicts=3, baseFailure="9" * 20, recheck="f" * 40)
    rounds.update(due=True, automatic=True)
    rounds["signatures"] = ["sha256:" + "f" * 64] * most("rounds.signatures")
    rounds["findings"] = [
        {"severity": "blocking" if n % 2 else "suggestion", "text": worst(LONGEST["finding"])}
        for n in range(most("rounds.findings"))
    ]
    if with_history:
        rounds["fixes"], rounds["lastReview"] = history()
        rounds["fixRound"] = {"auto": True, "diagnosis": True, "note": clipped(HISTORY["note"])}
    rounds["criteria"] = ["unverifiable"] * most("rounds.criteria")
    rounds["checklist"] = ["not_applicable"] * most("rounds.checklist")
    asked = ["split", "titles", "parts", "fewer", "answers", "replan", "supersede"]
    state["asked"] = (asked * 4)[: most("asked")]
    state["parts"] = [{"item": str(4300 + n), "state": None} for n in range(most("parts"))]
    parts = [
        {**part, "body": {"name": f"part-{part['key']}", "ref": "9" * 20}}
        for part in state["plan"]["split"]
    ]
    state["pending"] = [
        {"id": "a99.1", "kind": "createParts", "next": None, "parts": parts},
        {"id": "a99.2", "kind": "needPerson", "next": None, "reason": "roundsUsedUp"},
        {
            "id": "a99.3",
            "kind": "show",
            "next": None,
            "what": "plan",
            "text": {"name": "a", "ref": "b"},
        },
        {"id": "a99.4", "kind": "runChecks", "next": None, "changeVersion": "f" * 40},
    ]
    state["pending"][1].update(text=worst(LONGEST["reason"]), ref="9" * 20)
    # The rest of the outbox: a person is told one thing at a time, since a notice is taken
    # back when attention changes, so what piles up while a host has not reported is cancels.
    state["pending"] += [
        {"id": f"a99.{n}", "kind": "cancel", "next": None, "action": f"a98.{n}"}
        for n in range(5, most("pending") + 1)
    ]
    state["recentEvents"] = [f"event-{n:040}" for n in range(most("recentEvents"))]
    return state


def decisions(state):
    """What the engine decides from, which fitting a state must keep."""
    plan = state["plan"]
    return (
        state["state"],
        state["title"],
        state["depth"],
        state["attention"]["reason"],
        [(q["recommended"], q.get("closes"), len(q["options"])) for q in plan["questions"]],
        [
            (q["recommended"], q["closes"], q["replans"], len(q["options"]))
            for q in state["questions"]["items"]
        ],
        state["questions"]["noChange"],
        plan["split"],
        len(plan["protectedPaths"]),
        [item["severity"] for item in state["rounds"]["findings"]],
        [
            (record["round"], record["auto"], record["diagnosis"], len(record["failing"]))
            for record in state["rounds"].get("fixes", [])
        ],
        state["rounds"].get("fixRound"),
        [
            (item["severity"], item["line"])
            for item in state["rounds"].get("lastReview", {}).get("findings", [])
        ],
        {
            key: state["rounds"].get("lastReview", {}).get(key)
            for key in ("round", "version", "verdict", "leftOut")
        },
        state["rounds"]["signatures"],
        state["rounds"]["baseFailure"],
        state["rounds"]["recheck"],
        state["rounds"]["due"],
        state["rounds"]["automatic"],
        state["rounds"]["criteria"],
        state["rounds"]["checklist"],
        state["parts"],
        state["pending"],
    )


# What the state-document bullet of docs/engine.md says of each bound the code sets, by its name
# here: the page's phrase, with `{}` where it writes the value.
BOUNDS = {
    "STATE_LIMIT": "{} bytes as JSON",
    "TEXT_LIMIT": "one GitHub comment of {} characters",
    "MOST.questions": "{} questions of",
    "MOST.options": "of {} options each",
    "MOST.protectedPaths": "{} protected paths",
    "MOST.split": "{} split parts",
    "MOST.findings": "{} findings with the blocking ones first",
    "MOST.signatures": "{} failure signatures",
    "MOST.failing": "{} failing checks, each",
    "KEY": "{}-digit digest",
    "MOST.repeated": "{} checks that repeated",
    "MOST.criteria": "{} criteria and",
    "MOST.checklist": "{} checklist answers",
    "LONGEST.question": "a question at {} characters",
    "LONGEST.option": "an option at {}",
    "LONGEST.title": "a part's at {}",
    "LONGEST.path": "a path at {}",
    "LONGEST.finding": "a finding at {}",
    "LONGEST.reason": "a reason for a person at {}",
    "LONGEST.check": "a check's name at {}",
    "HISTORY.note": "a note at {} characters",
    "HISTORY.summary": "a summary at {}",
    "HISTORY.cause": "a cause at {}",
    "HISTORY.body": "body at {}",
    "HISTORY.signature": "fix record at {}",
    "MOST.fixes": "the latest {} fix records",
    "MOST.fixFailing": "at most {} failing checks",
    "lastReview": "the last review's {} findings",
    "DIAGNOSES": "past the {} that a diagnosis round",
}


def bound(name):
    """The value the code sets for a name of BOUNDS."""
    if name == "lastReview":
        return most("rounds.lastReview.findings")
    table, _, key = name.partition(".")
    if key:
        return {"MOST": MOST, "LONGEST": LONGEST, "HISTORY": HISTORY}[table][key]
    return {
        "STATE_LIMIT": STATE_LIMIT,
        "TEXT_LIMIT": TEXT_LIMIT,
        "KEY": KEY,
        "DIAGNOSES": DIAGNOSES,
    }[name]


class SizeTest(unittest.TestCase):
    def test_the_page_names_the_bounds_of_the_code(self):
        text = (ROOT / "docs" / "engine.md").read_text(encoding="utf-8")
        bullet = text.split("\n- **It has a size.**", 1)[1].split("\n- ", 1)[0]
        bullet = " ".join(bullet.split())
        for group, table in (("MOST", MOST), ("LONGEST", LONGEST), ("HISTORY", HISTORY)):
            named = {name.split(".")[1] for name in BOUNDS if name.startswith(f"{group}.")}
            self.assertEqual(named, set(table))
        for name in BOUNDS:
            with self.subTest(name):
                self.assertIn(BOUNDS[name].format(f"{bound(name):,}"), bullet)
        # NexKit's own name and the names of encodings hold digits that are not limits.
        rest = re.sub(r"NexKit \d+|UTF-8|base64", "", bullet)
        numbers = re.findall(r"(?<![\w-])\d+(?:,\d{3})*", rest)
        self.assertEqual(
            sorted(int(number.replace(",", "")) for number in numbers),
            sorted(bound(name) for name in BOUNDS),
        )

    def test_a_document_at_the_limit_fits_one_comment(self):
        state = {**blank("42"), "revision": 1}
        state["attention"] = {"reason": "failed", "text": "", "ref": None}
        state["attention"]["text"] = "x" * (STATE_LIMIT - size(state))
        self.assertEqual(size(state), STATE_LIMIT)
        body = item_state.render(state)
        self.assertLessEqual(len(body), TEXT_LIMIT)
        comment = {"id": 1, "body": body, "user": {"login": BOT_LOGIN}}
        self.assertEqual(item_state.find([comment])[1], state)

    def test_a_state_comment_stored_with_used_usd_is_read_without_it(self):
        state = {**blank("42"), "revision": 1}
        old = copy.deepcopy(state)
        old["used"]["usd"] = 2.37
        self.assertNotEqual(protocol.check(old, "state.schema.json"), [])
        data = base64.b64encode(encoded(old)).decode()
        body = f"### NexKit state\n\n{item_state.PREFIX}{data} -->"
        comment = {"id": 1, "body": body, "user": {"login": BOT_LOGIN}}
        self.assertEqual(item_state.find([comment])[1], state)

    def test_the_largest_state_the_schema_allows_is_shortened_to_fit(self):
        state = largest()
        self.assertEqual(protocol.check(state, "state.schema.json"), [])
        before = decisions(state)
        self.assertGreater(size(state), STATE_LIMIT)
        fitted = fit(state)
        self.assertIsNotNone(fitted)
        self.assertLessEqual(size(fitted), STATE_LIMIT)
        self.assertEqual(protocol.check(fitted, "state.schema.json"), [])
        self.assertEqual(decisions(fitted), before)
        self.assertLessEqual(len(item_state.render(fitted)), TEXT_LIMIT)
        # The history that the prompts show was shortened last: five fix records and 30
        # findings are all still there, and so are the checks that still failed after each.
        rounds = fitted["rounds"]
        self.assertEqual(len(rounds["fixes"]), MOST["fixes"])
        self.assertEqual(len(rounds["lastReview"]["findings"]), MOST["findings"])
        self.assertEqual(
            [len(record["failing"]) for record in rounds["fixes"]], [MOST["fixFailing"]] * 5
        )

    def test_the_other_texts_are_shortened_before_the_history_of_the_rounds(self):
        state = largest(with_history=False)
        before = decisions(state)
        fitted = fit(state)
        self.assertLessEqual(size(fitted), STATE_LIMIT)
        # The findings went first: what fits of the attention's text was kept.
        self.assertTrue(all(len(item["text"]) <= 3 for item in fitted["rounds"]["findings"]))
        self.assertGreater(len(fitted["attention"]["text"]), 3)
        self.assertEqual(decisions(fitted), before)
        # With the history as long as it can be, they are cut to nothing before it is cut.
        state = largest()
        found = fit(state)
        self.assertEqual(len(found["attention"]["text"]), 3)
        self.assertTrue(all(item["text"] == "..." for item in found["rounds"]["findings"]))

    def test_long_round_texts_are_shortened_after_the_others_and_keep_what_decides(self):
        state = {**blank("42"), "revision": 1}
        rounds = state["rounds"]
        rounds.update(started=9, counted=2, conflicts=1, signatures=["sha256:" + "a" * 64] * 3)
        rounds.update(criteria=["unmet"], checklist=["not_met"], failing=["f" * 12] * 3)
        failing = [{"name": "test", "signature": "g" * HISTORY["signature"]}] * 3
        rounds["fixes"] = [
            {
                "round": number,
                "auto": bool(number % 2),
                "diagnosis": False,
                "note": "n" * HISTORY["note"],
                "summary": "s" * HISTORY["summary"],
                "cause": "c" * HISTORY["cause"],
                "failing": copy.deepcopy(failing),
            }
            for number in range(5, 10)
        ]
        rounds["lastReview"] = {
            "round": 9,
            "version": "f" * 40,
            "verdict": "request_changes",
            "findings": [
                {
                    "severity": "blocking" if number % 2 else "suggestion",
                    "file": f"file{number}.py",
                    "line": number,
                    "body": "b" * HISTORY["body"],
                }
                for number in range(MOST["findings"])
            ],
            "leftOut": 4,
        }

        def kept(found):
            rounds = found["rounds"]
            review = rounds["lastReview"]
            return (
                {key: value for key, value in rounds.items() if key not in ("fixes", "lastReview")},
                [
                    {
                        key: value
                        for key, value in fix.items()
                        if key in ("round", "auto", "diagnosis", "failing")
                    }
                    for fix in rounds["fixes"]
                ],
                {key: value for key, value in review.items() if key != "findings"},
                [(item["severity"], item["file"], item["line"]) for item in review["findings"]],
            )

        self.assertEqual(protocol.check(state, "state.schema.json"), [])
        before = kept(state)
        self.assertGreater(size(state), STATE_LIMIT)
        fitted = fit(state)
        self.assertIsNotNone(fitted)
        self.assertLessEqual(size(fitted), STATE_LIMIT)
        self.assertEqual(protocol.check(fitted, "state.schema.json"), [])
        # Counts, the signatures that rounds compare and each record's checks are unchanged.
        self.assertEqual(kept(fitted), before)
        self.assertEqual(fitted["rounds"]["signatures"], ["sha256:" + "a" * 64] * 3)
        # The last review's findings were shortened first, and the fix texts stayed whole.
        bodies = [item["body"] for item in fitted["rounds"]["lastReview"]["findings"]]
        self.assertTrue(all(len(body) < HISTORY["body"] for body in bodies))
        self.assertEqual(
            {len(fix["summary"]) for fix in fitted["rounds"]["fixes"]}, {HISTORY["summary"]}
        )

    def test_a_state_that_fits_is_left_as_it_is(self):
        state = blank("42")
        self.assertEqual(dump(fit(copy.deepcopy(state))), dump(state))

    def test_what_an_agent_returns_never_takes_the_state_past_the_limit(self):
        long = "\U0001f600" * 20_000
        output = json.loads((protocol.ROOT / "examples" / "results" / "plan.json").read_text())
        many = [{"question": long, "options": [long] * 9, "recommended": 8, "closes": 2}] * 12
        split = [{"title": long, "body": "A part.", "depends_on": list(range(20_000))}] * 10
        output.update(questions=many, protected_paths=[long] * 25, split=split, too_large=True)
        state, actions, _ = step(None, CREATED)
        state, actions, _ = step(state, PLAN)
        agent = {
            "tokens": None,
            "limit": None,
            "transient": False,
            "noResult": False,
            "error": None,
            "session": None,
            "changeVersion": None,
            "timedOut": False,
            "seconds": None,
        }
        finished = event(
            "actionFinished",
            5,
            action=actions[0]["id"],
            changeVersion=None,
            result={**agent, "output": output},
        )
        state, _, _ = step(state, finished)
        self.assertLessEqual(size(state), STATE_LIMIT)
        self.assertEqual(protocol.check(state, "state.schema.json"), [])
        plan = state["plan"]
        self.assertEqual(len(plan["questions"]), MOST["questions"])
        self.assertEqual(len(plan["protectedPaths"]), MOST["protectedPaths"])
        # Each part needs only the earlier parts, and its title is clipped, never cut further.
        self.assertEqual([part["needs"] for part in plan["split"]][:3], [[], [1], [1, 2]])
        self.assertEqual({len(part["title"]) for part in plan["split"]}, {LONGEST["title"] + 3})
        # The recommended option was past the options kept, so the state names none.
        self.assertEqual({(q["recommended"], q["closes"]) for q in plan["questions"]}, {(0, 2)})
        # A split of more parts than the state keeps is kept as its count alone.
        output.update(split=[{"title": "A part", "body": "A part.", "depends_on": []}] * 3000)
        state, actions, _ = step(None, CREATED)
        state, actions, _ = step(state, PLAN)
        finished["result"] = {**agent, "output": output}
        finished["action"] = actions[0]["id"]
        state, _, _ = step(state, finished)
        self.assertEqual((state["plan"]["parts"], state["plan"]["split"]), (3000, []))

    def test_a_review_and_checks_past_the_lists_keep_what_decides(self):
        state = json.loads((protocol.ROOT / "examples" / "state" / "checking.json").read_text())
        action = state["pending"][0]
        checks = [
            {
                "name": f"check {n}",
                "passed": False,
                "skipped": False,
                "signature": "x" * 10_000,
                "knownBaseFailure": False,
            }
            for n in range(100)
        ]
        finished = event(
            "actionFinished",
            6,
            item=state["item"],
            action=action["id"],
            changeVersion=action["changeVersion"],
            result={"checks": checks},
        )
        state, actions, _ = step(state, finished)
        signatures = state["rounds"]["signatures"]
        self.assertEqual(len(signatures), MOST["signatures"])
        # A long signature is named by its hash: the same signature, the same name.
        self.assertEqual(len(set(signatures)), 1)
        self.assertTrue(signatures[0].startswith("sha256:"))
        self.assertLessEqual(size(state), STATE_LIMIT)

    def test_a_state_past_the_limit_is_never_stored_cut(self):
        state = {**blank("42"), "revision": 1}
        state["attention"] = {"reason": "failed", "text": "x" * STATE_LIMIT, "ref": None}
        with self.assertRaises(item_state.StateError):
            item_state.render(state)


class FailingChecksSizeTest(unittest.TestCase):
    """The failing checks that rounds compare, and those that repeated, fit the limit too."""

    def test_the_largest_state_with_the_failing_checks_is_shortened_to_fit(self):
        state = largest()
        rounds = state["rounds"]
        rounds["failing"] = ["f" * 12] * MOST["failing"]
        rounds["repeated"] = [worst(LONGEST["check"])] * MOST["repeated"]
        rounds["fixed"] = "f" * 40
        self.assertEqual(protocol.check(state, "state.schema.json"), [])
        found = fit(copy.deepcopy(state))
        self.assertIsNotNone(found)
        self.assertLessEqual(size(found), STATE_LIMIT)
        self.assertEqual(found["rounds"]["failing"], rounds["failing"])
        self.assertEqual(found["rounds"]["fixed"], rounds["fixed"])
        # What still does not fit once every history text is cut: the oldest fix records leave
        # out which checks still failed after them, one after the other, as unknown.
        failing = ["failing" in record for record in found["rounds"]["fixes"]]
        self.assertEqual(failing, sorted(failing))
        self.assertTrue(failing[-1])
        self.assertFalse(failing[0])
        # Only the names past those a diagnosis round looks into are shortened.
        self.assertEqual(found["rounds"]["repeated"][:10], rounds["repeated"][:10])

    def test_nexkit_s_own_names_of_repeated_checks_are_kept_whole(self):
        state = largest(with_history=False)
        rounds = state["rounds"]
        rounds["failing"] = ["f" * 12] * MOST["failing"]
        rounds["repeated"] = ["c" * 40] * MOST["repeated"]
        found = fit(copy.deepcopy(state))
        self.assertEqual(found["rounds"]["repeated"], rounds["repeated"])


if __name__ == "__main__":
    unittest.main()
