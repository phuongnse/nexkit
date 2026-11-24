"""Each work item's state document in a comment of NexKit's App
(docs/engine.md#the-state-document): written only as the next revision of the one read, and
never past GitHub's comment size."""

import copy
import json
import unittest

from nexkit import engine, item_state, protocol
from nexkit.engine.machine import blank
from nexkit.protocol.size import LONGEST, MOST, STATE_LIMIT, dump, fit, size
from nexkit.state import TEXT_LIMIT
from tests.support import BOT_LOGIN, FakeGitHub

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
        schema = schema["properties"][part]
        schema = schema["anyOf"][0] if "anyOf" in schema else schema
    return schema["maxItems"]


# A text in the characters that take the most bytes: four in UTF-8, or two once JSON writes
# a quote or a backslash.
WORST = '\U0001f600"\\'


def worst(characters):
    return (WORST * characters)[:characters]


def largest():
    """The largest state the schema allows: every list as long as its `maxItems`, each text
    as long as the engine keeps it, in the characters that take the most bytes, and in the
    outbox the actions that hold texts, whole."""
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
    rounds = state["rounds"]
    rounds.update(started=13, counted=10, conflicts=3)
    rounds["signatures"] = ["sha256:" + "f" * 64] * most("rounds.signatures")
    rounds["findings"] = [
        {"severity": "blocking" if n % 2 else "suggestion", "text": worst(LONGEST["finding"])}
        for n in range(most("rounds.findings"))
    ]
    rounds["criteria"] = ["unverifiable"] * most("rounds.criteria")
    rounds["checklist"] = ["not_applicable"] * most("rounds.checklist")
    state["asked"] = (["split", "titles", "parts", "fewer", "answers"] * 4)[: most("asked")]
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
        plan["split"],
        len(plan["protectedPaths"]),
        [item["severity"] for item in state["rounds"]["findings"]],
        state["rounds"]["signatures"],
        state["rounds"]["criteria"],
        state["rounds"]["checklist"],
        state["parts"],
        state["pending"],
    )


class SizeTest(unittest.TestCase):
    def test_a_document_at_the_limit_fits_one_comment(self):
        state = {**blank("42"), "revision": 1}
        state["attention"] = {"reason": "failed", "text": "", "ref": None}
        state["attention"]["text"] = "x" * (STATE_LIMIT - size(state))
        self.assertEqual(size(state), STATE_LIMIT)
        body = item_state.render(state)
        self.assertLessEqual(len(body), TEXT_LIMIT)
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
        # The findings went first: what fits of the attention's text was kept.
        self.assertTrue(all(len(item["text"]) <= 3 for item in fitted["rounds"]["findings"]))
        self.assertGreater(len(fitted["attention"]["text"]), 3)

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
            "usd": None,
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


if __name__ == "__main__":
    unittest.main()
