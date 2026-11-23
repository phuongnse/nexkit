"""Each work item's state document in a comment of NexKit's App
(docs/engine.md#the-state-document): written only on the revision read, and never past
GitHub's comment size."""

import copy
import json
import unittest

from nexkit import engine, item_state, protocol
from nexkit.engine.machine import blank
from nexkit.protocol.size import MOST, STATE_LIMIT, dump, fit, size
from nexkit.state import TEXT_LIMIT
from tests.support import FakeGitHub

PERSON = {"id": "octocat", "role": "decides"}


def event(kind, **fields):
    return {"protocol": 1, "kind": kind, "item": "42", "at": "2026-11-23T15:01:11Z", **fields}


CREATED = event(
    "created",
    itemKind="change",
    title="A work item",
    text={"name": "body", "ref": "42"},
    actors={"plan": "engine", "change": "engine", "review": "engine"},
    plan=None,
    change=None,
)
PLAN = event("requested", what="plan", by=PERSON, note="")
HELD = event("held", label="on-hold")
RELEASED = event("released", label="on-hold")


def step_on(event):
    """An answer of the engine to `event`, as `item_state.apply` calls it."""
    return lambda state: engine.step(state, event, {})


def created(gh):
    state, _, _ = engine.step(None, CREATED, {})
    assert item_state.write(gh, 42, state, None)
    return state


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
        first = created(gh)
        held, _, _ = engine.step(first, HELD, {})
        self.assertTrue(item_state.write(gh, 42, held, first["revision"]))
        # A second answer to the state of revision 1, which is no longer stored.
        late, _, _ = engine.step(first, PLAN, {})
        self.assertFalse(item_state.write(gh, 42, late, first["revision"]))
        self.assertEqual(item_state.read(gh, 42)[1], held)
        # And a first write when a state is already stored.
        self.assertFalse(item_state.write(gh, 42, first, None))
        self.assertEqual(item_state.read(gh, 42)[1], held)

    def test_an_event_whose_write_lost_is_sent_again_on_the_stored_state(self):
        gh = FakeGitHub()
        created(gh)
        seen = []

        def answer(state):
            seen.append(state)
            if len(seen) == 1:
                # Another job stores its answer to a hold after this one read the state.
                other, _, _ = engine.step(state, HELD, {})
                self.assertTrue(item_state.write(gh, 42, other, state["revision"]))
            return engine.step(state, PLAN, {})

        state, actions = item_state.apply(gh, 42, answer)
        self.assertEqual([found["revision"] for found in seen], [1, 2])
        self.assertTrue(seen[1]["hold"])
        self.assertEqual((state["state"], state["hold"], state["revision"]), ("planning", True, 3))
        self.assertEqual([action["kind"] for action in actions], ["runAgent"])
        self.assertEqual(item_state.read(gh, 42)[1], state)
        self.assertEqual(len(gh.comments(42)), 1)

    def test_writes_that_keep_coming_first_stop_the_event(self):
        gh = FakeGitHub()
        created(gh)

        def answer(state):
            other, _, _ = engine.step(state, RELEASED if state["hold"] else HELD, {})
            item_state.write(gh, 42, other, state["revision"])
            return engine.step(state, PLAN, {})

        with self.assertRaises(item_state.Moved):
            item_state.apply(gh, 42, answer)
        self.assertEqual(item_state.read(gh, 42)[1]["state"], "new")

    def test_an_answer_that_changes_nothing_writes_nothing(self):
        gh = FakeGitHub()
        state = created(gh)
        before = copy.deepcopy(gh.issue_comments[42])
        found, actions = item_state.apply(gh, 42, step_on(event("tick", wait="none")))
        self.assertEqual((found, actions), (state, []))
        self.assertEqual(gh.issue_comments[42], before)

    def test_of_two_first_writes_the_older_comment_is_the_state(self):
        gh = FakeGitHub()
        state, _, _ = engine.step(None, CREATED, {})
        other, _, _ = engine.step(state, HELD, {})
        posted = gh.comment

        def racing(number, body):
            # The other first write posts between this one's read and its post.
            gh.comment = posted
            posted(number, item_state.render(other))
            return posted(number, body)

        gh.comment = racing
        self.assertFalse(item_state.write(gh, 42, state, None))
        self.assertEqual(item_state.read(gh, 42)[1], other)
        self.assertEqual(gh.comments(42)[1]["body"], item_state.LOST_TEXT)

    def test_only_a_marker_of_the_app_where_it_ends_the_comment_counts(self):
        gh = FakeGitHub()
        body = item_state.render(blank("42"))
        gh.human_comment(42, body)
        gh.comment(42, f"{body}\n\nA quote after the marker.")
        self.assertEqual(item_state.read(gh, 42), (None, None))

    def test_a_marker_that_holds_no_state_document_is_an_error(self):
        gh = FakeGitHub()
        gh.comment(42, f"{item_state.PREFIX}e30= -->")
        with self.assertRaises(item_state.StateError):
            item_state.read(gh, 42)


def largest():
    """A state at every limit the engine keeps: every list as long as MOST allows, ten parts
    and split parts, and each text far longer than fits, in characters that JSON writes as
    six or twelve: the largest that the engine and the import can be asked to keep."""
    state = blank("4" * 20)
    long = "é" * 3000 + "\U0001f600" * 3000 + '"\\\n'
    state.update(
        state="ready",
        change={"id": "9" * 20, "version": "f" * 40},
        attention={"reason": "roundsUsedUp", "text": long},
        hold=True,
    )
    state["waits"] = [
        {"id": f"w{n}", "for": "time", "until": "2026-11-23T15:01:11Z", "on": None}
        for n in range(10)
    ]
    state["pending"] = [
        {"id": f"a9.{n}", "kind": "runAgent", "changeVersion": "f" * 40} for n in range(10)
    ]
    question = {
        "question": long,
        "options": [long] * MOST["options"],
        "recommended": 1,
        "closes": MOST["options"],
    }
    state["plan"] = {
        "text": {"name": "plan", "ref": "9" * 20},
        "questions": [copy.deepcopy(question) for _ in range(MOST["questions"])],
        "criteria": 50,
        "tooLarge": True,
        "parts": 10,
        "noChange": False,
        "protectedPaths": [long] * MOST["protectedPaths"],
        "approved": True,
        "split": [{"key": n, "title": long, "needs": list(range(1, n))} for n in range(1, 11)],
    }
    rounds = state["rounds"]
    rounds.update(started=13, counted=10, conflicts=3)
    rounds["signatures"] = [long] * MOST["signatures"]
    rounds["findings"] = [
        {"severity": "blocking" if n % 2 else "suggestion", "text": long[:300]}
        for n in range(MOST["findings"])
    ]
    rounds["criteria"] = ["unverifiable"] * 50
    rounds["checklist"] = ["not_met"] * 50
    state["asked"] = ["split", "titles", "parts", "fewer", "answers"]
    state["parts"] = [{"item": str(4300 + n), "state": None} for n in range(10)]
    return state


def decisions(state):
    """What the engine decides from, which fitting a state must keep."""
    plan = state["plan"]
    return (
        state["state"],
        state["attention"]["reason"],
        [(q["recommended"], q.get("closes"), len(q["options"])) for q in plan["questions"]],
        [(part["key"], part["needs"]) for part in plan["split"]],
        len(plan["protectedPaths"]),
        [item["severity"] for item in state["rounds"]["findings"]],
        len(state["rounds"]["signatures"]),
        state["rounds"]["criteria"],
        state["rounds"]["checklist"],
        state["parts"],
    )


class SizeTest(unittest.TestCase):
    def test_the_largest_state_document_fits_one_comment(self):
        # A document of exactly the protocol's limit, the most the schema allows.
        state = blank("42")
        state["attention"] = {"reason": "failed", "text": ""}
        state["attention"]["text"] = "x" * (STATE_LIMIT - size(state))
        self.assertEqual(size(state), STATE_LIMIT)
        body = item_state.render(state)
        self.assertLessEqual(len(body), TEXT_LIMIT)
        comment = {"id": 1, "body": body, "user": {"login": "nexkit-acme[bot]"}}
        self.assertEqual(item_state.find([comment])[1], state)

    def test_the_largest_state_the_engine_keeps_is_shortened_to_fit(self):
        state = largest()
        before = decisions(state)
        self.assertGreater(size(state), STATE_LIMIT)
        fitted = fit(state)
        self.assertLessEqual(size(fitted), STATE_LIMIT)
        self.assertEqual(protocol.check(fitted, "state.schema.json"), [])
        self.assertEqual(decisions(fitted), before)
        self.assertLessEqual(len(item_state.render(fitted)), TEXT_LIMIT)
        # The findings went first: what fits of the other texts was kept.
        self.assertTrue(all(len(item["text"]) <= 3 for item in fitted["rounds"]["findings"]))
        self.assertGreater(len(fitted["attention"]["text"]), 3)

    def test_a_state_that_fits_is_left_as_it_is(self):
        state = blank("42")
        self.assertEqual(dump(fit(copy.deepcopy(state))), dump(state))

    def test_the_engine_never_answers_a_state_past_the_limit(self):
        long = "\U0001f600" * 5000
        output = json.loads((protocol.ROOT / "examples" / "results" / "plan.json").read_text())
        output.update(
            questions=[{"question": long, "options": [long] * 9, "recommended": 8, "closes": 2}]
            * 12,
            protected_paths=[long] * 25,
        )
        state, actions, _ = engine.step(None, CREATED, {})
        state, actions, _ = engine.step(state, PLAN, {})
        result = {
            "output": output,
            "tokens": None,
            "limit": None,
            "transient": False,
            "noResult": False,
            "error": None,
            "session": None,
            "changeVersion": None,
            "timedOut": False,
        }
        finished = event(
            "actionFinished", action=actions[0]["id"], changeVersion=None, result=result
        )
        request = {"protocol": 1, "call": "step", "state": state, "event": finished, "policies": {}}
        answer = engine.answer(request)
        self.assertNotIn("error", answer)
        state = answer["state"]
        self.assertLessEqual(size(state), STATE_LIMIT)
        plan = state["plan"]
        self.assertEqual(len(plan["questions"]), MOST["questions"])
        self.assertEqual(len(plan["protectedPaths"]), MOST["protectedPaths"])
        # The recommended option was past the options kept, so the state names none.
        self.assertEqual({(q["recommended"], q["closes"]) for q in plan["questions"]}, {(0, 2)})
        self.assertEqual(state["attention"]["reason"], "question")

    def test_a_state_past_the_limit_is_never_stored_cut(self):
        state = blank("42")
        state["attention"] = {"reason": "failed", "text": "x" * 60_000}
        with self.assertRaises(item_state.StateError):
            item_state.render(state)


if __name__ == "__main__":
    unittest.main()
