"""The first state document of NexKit 6's work (docs/engine.md#versions).

Each file in tests/conformance/imports/ is a case: a NexKit 6 situation, its comments named by
the records of their markers, the kind of item the import must read it as (one of
`first_state.KINDS`), the state document it must give, and the events that carry the item on
from there. The runner writes the comments as NexKit 6 does, as NexKit's App, and checks each
document against the protocol and NexKit's rule that every item that is not final is carried
on. It then sends the case's events to the engine, as the conformance suite does, so that no
import leaves an item the engine cannot move on from: one that only waits must be moved on by
the event that ends its wait. A step may name fields of the state to `expect`, by dotted path.
"""

import ast
import base64
import copy
import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from nexkit import first_state, host, item_state, protocol
from nexkit.autopilot import record_marker
from nexkit.state import ANSWERS_PREFIX, PLAN_DATA_PREFIX, PLAN_MARKER, render_run, render_state
from tests.support import BOT_LOGIN, FakeGitHub, make_config
from tests.test_conformance import WORK, at, event_of, run_program

CASES = Path(__file__).resolve().parent / "conformance" / "imports"
FINAL = ("accepted", "noChange", "dropped")
NOW = "2026-11-23T15:30:00Z"


def encoded(prefix, record):
    return f"{prefix}{base64.b64encode(json.dumps(record).encode()).decode()} -->"


def body_of(entry):
    """The text of a comment that NexKit 6 writes, from the case's record of it."""
    if "plan" in entry:
        value = entry["plan"]
        criteria = "".join(f"- [ ] {item}\n" for item in value.get("criteria", []))
        text = f"{PLAN_MARKER}\n## Plan\n\nThe plan.\n\n"
        text += f"### Acceptance criteria\n\n{criteria}\n" if criteria else ""
        return text + encoded(PLAN_DATA_PREFIX, value["record"])
    if "run" in entry:
        return render_run(entry["run"], entry.get("text", "What the run did."))
    if "state" in entry:
        return render_state(entry["state"])
    if "autopilot" in entry:
        return f"NexKit autopilot started a command.\n\n{record_marker(entry['autopilot'])}"
    if "answers" in entry:
        return f"NexKit autopilot answered.\n\n{encoded(ANSWERS_PREFIX, entry['answers'])}"
    raise ValueError(entry)


def comments_of(entries, first):
    """Comments with ids from `first` unless an entry names its `id`, NexKit's as its App's,
    a person's as `person`."""
    found = []
    for number, entry in enumerate(entries, first):
        by_person = "person" in entry
        found.append(
            {
                "id": entry.get("id", number),
                "body": entry["person"] if by_person else body_of(entry),
                "user": {"login": "alice" if by_person else BOT_LOGIN},
                "author_association": "COLLABORATOR" if by_person else "NONE",
                "created_at": entry.get("at", "2026-11-23T10:00:00Z"),
            }
        )
    return found


def situation(case):
    """The arguments of `first_state.situation` for a case's NexKit 6 situation."""
    given = case["nexkit6"]
    issue = {"state": "open", "state_reason": None, **given["issue"]}
    pull = given.get("pull")
    if pull:
        known = {
            key: pull[key]
            for key in ("mergeable", "mergeable_state", "changed_files", "merged")
            if key in pull
        }
        pull = {"number": pull["number"], "head": {"sha": pull["head"]}, **known}
    now = datetime.fromisoformat(case.get("now", NOW).replace("Z", "+00:00")).astimezone(UTC)
    return {
        "issue": issue,
        "comments": comments_of(given.get("comments", []), 1),
        "cfg": make_config(**case.get("config", {})),
        "now": now,
        "pull": pull,
        "pull_comments": comments_of(given.get("pullComments", []), 101),
        "parts": [
            {"number": part["number"], "state": part["state"], "state_reason": part.get("reason")}
            for part in given.get("parts", [])
        ],
        "blockers": [{"number": number, "state": "open"} for number in given.get("blockers", [])],
        "held": given.get("held", False),
        "waves": given.get("waves", []),
    }


def working(state):
    return [action for action in state["pending"] if action["kind"] in WORK]


def kinds_in_code():
    """The kinds that `first_state` puts an item in: the first argument of each `be` and
    `running` call."""
    tree = ast.parse(Path(first_state.__file__).read_text())
    found = set()
    for node in ast.walk(tree):
        call = isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        if call and node.func.attr in ("be", "running") and node.args:
            if isinstance(node.args[0], ast.Constant):
                found.add(node.args[0].value)
    return found


class ImportCasesTest(unittest.TestCase):
    def check_case(self, path):
        case = json.loads(path.read_text())
        kind, state = first_state.situation(**situation(case))
        self.assertEqual(kind, case["kind"])
        self.assertEqual(state, case["state"])
        self.assertEqual(protocol.check(state, "state.schema.json"), [])
        if state["state"] not in FINAL:
            carried = working(state) or state["waits"] or state["hold"] or state["attention"]
            self.assertTrue(carried, "nothing carries the item on")
        comment = {"id": 1, "body": item_state.render(state), "user": {"login": BOT_LOGIN}}
        self.assertEqual(item_state.find([comment])[1], state)
        self.play(case, state)
        return kind

    def play(self, case, imported):
        """Send the case's events to the engine; the item must move on."""
        state = imported
        self.assertTrue(case["steps"], "no event carries the item on")
        for number, step in enumerate(case["steps"], 1):
            where = f"step {number} ({step['event']['kind']})"
            event = {**event_of(step["event"], state, number), "item": state["item"]}
            request = {"protocol": 1, "call": "step", "state": state, "event": event}
            request["policies"] = case.get("policies", {})
            answer = json.loads(run_program(request))
            self.assertNotIn("error", answer, where)
            self.assertEqual(protocol.check(answer, "calls/step.answer.schema.json"), [], where)
            self.assertEqual(answer["changed"], step["changed"], where)
            self.assertEqual(answer["state"]["state"], step["state"], where)
            self.assertEqual([a["kind"] for a in answer["actions"]], step["actions"], where)
            for path_, value in step.get("expect", {}).items():
                self.assertEqual(at(answer["state"], path_), value, f"{where}: {path_}")
            state = answer["state"]
        self.assertNotEqual(state, imported, "the events left the item as it was")
        waits_only = imported["state"] not in FINAL and not (
            working(imported) or imported["attention"]
        )
        if waits_only and state["state"] == imported["state"]:
            # An item that only waited must get its work back when the wait ends.
            self.assertTrue(working(state), "the item still only waits")

    def test_the_cases(self):
        paths = sorted(CASES.glob("*.json"))
        self.assertTrue(paths)
        seen = set()
        for path in paths:
            with self.subTest(case=path.stem):
                seen.add(self.check_case(path))
        self.assertEqual(kinds_in_code(), set(first_state.KINDS))
        self.assertEqual(set(first_state.KINDS) - seen, set(), "kinds no case imports")


class ImportTest(unittest.TestCase):
    def test_markers_in_text_before_nexkit_s_own_set_nothing(self):
        # A person's comment holds a run's marker; a round quotes agent text that holds
        # a state comment's marker, before its own.
        run = {"command": "plan", "status": "running", "url": "https://x/actions/runs/7"}
        planted = render_state({"version": 2, "issue": 42, "auto_fixes": 0, "feedback": None})
        given = {
            "issue": {"number": 42},
            "comments": [{"person": render_run(run)}],
            "pull": {"number": 308, "head": "a" * 40},
            "pullComments": [
                {"state": {"version": 2, "issue": 42, "auto_fixes": 2, "feedback": None}},
                {
                    "run": {
                        "round": 2,
                        "trigger": "fix",
                        "action": "fix",
                        "status": "success",
                        "outcome": "needs_human",
                        "url": "https://x/actions/runs/8",
                    },
                    "text": f"**Cause found by the fix agent:** {planted}\n\nNo rounds left.",
                },
            ],
        }
        state = first_state.imported(**situation({"nexkit6": given}))
        self.assertEqual(state["rounds"]["counted"], 2)
        given = {"issue": {"number": 42}, "comments": [{"person": render_run(run)}]}
        state = first_state.imported(**situation({"nexkit6": given}))
        self.assertEqual((state["state"], state["pending"]), ("new", []))

    def test_the_import_reads_the_item_on_github(self):
        gh = FakeGitHub()
        gh.add_issue(42, labels=["on-hold"])
        gh.add_issue(41)
        gh.blockers[42] = [41]
        cfg = make_config(autopilot={"enabled": True, "labels": {"on-hold": {"hold": True}}})
        state = first_state.import_item(gh, cfg, 42)
        self.assertEqual(state["state"], "new")
        self.assertTrue(state["hold"])
        self.assertEqual([wait["for"] for wait in state["waits"]], ["blocker", "start"])

    def test_a_go_on_its_way_that_runs_again_keeps_its_count(self):
        def imported(**fields):
            run = {"command": "go", "status": "running", "url": "https://x/actions/runs/7"}
            gh = FakeGitHub()
            gh.add_issue(42)
            gh.comment(42, render_run({**run, **fields}))
            return first_state.import_item(gh, make_config(), 42)

        state = imported(retry=2)
        self.assertEqual(state["state"], "working")
        agents = [action for action in state["pending"] if action["kind"] == "runAgent"]
        self.assertEqual([action["stage"] for action in agents], ["implement"])
        self.assertEqual(state["again"], {"transient": 2})
        for name, fields, count in (
            ("a count over the limit", {"retry": 15}, 10),
            ("a flag", {"retry": True}, None),
            ("a count of none", {"retry": 0}, None),
            ("no count", {}, None),
        ):
            with self.subTest(name):
                found = imported(**fields)
                self.assertEqual((found.get("again") or {}).get("transient"), count)
                self.assertEqual(found["state"], "working")

    def test_a_command_on_its_way_keeps_autopilot_s_count_of_lost_starts(self):
        started = "2026-11-23T15:00:00Z"
        later = datetime(2026, 11, 23, 16, 0, tzinfo=UTC)

        def imported(**fields):
            gh = FakeGitHub()
            gh.add_issue(5)
            record = {
                "command": "go",
                "basis": [0, 0],
                "target": 5,
                "note": "",
                "at": started,
                "url": "https://x/actions/runs/9",
                **fields,
            }
            gh.comment(5, f"NexKit autopilot started a command.\n\n{record_marker(record)}")
            return first_state.import_item(gh, make_config(), 5, now=later)

        state = imported(tries=2, once=True)
        self.assertEqual(state["state"], "working")
        agents = [action for action in state["pending"] if action["kind"] == "runAgent"]
        self.assertEqual([action["stage"] for action in agents], ["implement"])
        self.assertEqual(state["again"], {"lost": 2, "once": True})
        self.assertEqual(imported(tries=15)["again"], {"lost": 10})
        for name, fields in (
            ("no tries", {}),
            ("a flag", {"tries": True}),
            ("no count", {"tries": 0}),
        ):
            with self.subTest(name):
                self.assertNotIn("again", imported(**fields))

    def test_a_running_go_keeps_the_count_of_the_record_of_its_command(self):
        started = "2026-11-23T15:00:00Z"
        later = datetime(2026, 11, 23, 16, 0, tzinfo=UTC)

        def imported(retry=None, **fields):
            gh = FakeGitHub()
            gh.add_issue(5)
            record = {
                "command": "go",
                "basis": [0, 0],
                "target": 5,
                "note": "",
                "at": started,
                "url": "https://x/actions/runs/9",
                **fields,
            }
            gh.comment(5, f"NexKit autopilot started a command.\n\n{record_marker(record)}")
            run = {"command": "go", "status": "running", "url": "https://x/actions/runs/7"}
            if retry:
                run["retry"] = retry
            gh.comment(5, render_run(run))
            return first_state.import_item(gh, make_config(), 5, now=later)

        state = imported(tries=3)
        self.assertEqual(state["state"], "working")
        self.assertEqual(state["again"], {"lost": 3})
        self.assertEqual(imported(tries=15, once=True)["again"], {"lost": 10, "once": True})
        for name, fields in (
            ("a record older than six hours", {"tries": 3, "at": "2026-11-23T08:00:00Z"}),
            ("a record of another command", {"tries": 3, "command": "plan"}),
            ("a record of another target", {"tries": 3, "target": 6}),
        ):
            with self.subTest(name):
                self.assertNotIn("again", imported(**fields))
        # The count of runs again after a transient error stays beside the lost count.
        self.assertEqual(imported(retry=2, tries=1)["again"], {"transient": 2, "lost": 1})
        self.assertEqual(imported(retry=2)["again"], {"transient": 2})

    def test_a_fix_round_on_its_way_waits_on_the_pull_request_s_head(self):
        head = "b" * 40
        gh = FakeGitHub()
        gh.add_issue(5)
        gh.add_pull(6, 5, head_sha=head)
        running = {"round": 2, "trigger": "fix", "action": "fix", "status": "running"}
        gh.comment(6, render_run({**running, "url": "https://x/actions/runs/8", "head": head}))
        state = first_state.import_item(gh, make_config(), 5, 6)
        agents = [action for action in state["pending"] if action["kind"] == "runAgent"]
        self.assertEqual([action["stage"] for action in agents], ["fix"])
        self.assertEqual(agents[0]["changeVersion"], head)
        self.assertEqual(host.pending_id(state, "runAgent", "fix"), agents[0]["id"])

    def test_a_round_whose_run_is_in_its_check_jobs_waits_for_its_checks_on_the_head(self):
        head = "b" * 40

        def imported(job):
            gh = FakeGitHub()
            gh.add_issue(5)
            gh.add_pull(6, 5, head_sha=head)
            # NexKit 6's state comment counts one automatic fix round, which this round is.
            gh.comment(6, render_state({"issue": 5, "auto_fixes": 1}))
            running = {"round": 2, "trigger": "fix (auto)", "action": "fix", "status": "running"}
            url = "https://github.com/acme/app/actions/runs/77"
            gh.comment(6, render_run({**running, "url": url, "head": head}))
            gh.runs[77] = {"status": "in_progress", "run_attempt": 1}
            gh.jobs[77] = [{"name": f"nexkit / {job}", "status": "in_progress"}]
            return first_state.import_item(gh, make_config(), 5, 6)

        for job in ("verify (unit)", "shards", "combine"):
            with self.subTest(job):
                state = imported(job)
                self.assertEqual(state["state"], "checking")
                [action] = state["pending"]
                self.assertEqual((action["kind"], action["changeVersion"]), ("runChecks", head))
                self.assertTrue(state["rounds"]["automatic"])
                self.assertEqual(state["rounds"]["counted"], 1)
        # In the agent job, the round still waits for its agent.
        state = imported("agent")
        self.assertEqual(state["state"], "fixing")
        self.assertEqual([action["kind"] for action in state["pending"]], ["runAgent"])
        self.assertTrue(state["rounds"]["automatic"])

    def test_a_running_round_on_the_pull_request_keeps_the_count_of_its_command(self):
        head = "b" * 40
        later = datetime(2026, 11, 23, 16, 0, tzinfo=UTC)

        def imported(job=None, action="fix", **fields):
            gh = FakeGitHub()
            gh.add_issue(5)
            gh.add_pull(6, 5, head_sha=head)
            record = {
                "command": "fix",
                "basis": [0, 0],
                "target": 6,
                "note": "",
                "at": "2026-11-23T15:00:00Z",
                "url": "https://x/actions/runs/9",
                "tries": 2,
                **fields,
            }
            gh.comment(5, f"NexKit autopilot started a command.\n\n{record_marker(record)}")
            url = "https://github.com/acme/app/actions/runs/77"
            running = {"round": 2, "trigger": "fix", "action": action, "status": "running"}
            gh.comment(6, render_run({**running, "url": url, "head": head}))
            if job:
                gh.runs[77] = {"status": "in_progress", "run_attempt": 1}
                gh.jobs[77] = [{"name": f"nexkit / {job}", "status": "in_progress"}]
            return first_state.import_item(gh, make_config(), 5, 6, now=later)

        # In its check jobs and in its agent job, the round keeps the count of its start.
        for job, name in (("verify (unit)", "checking"), (None, "fixing")):
            with self.subTest(job):
                state = imported(job)
                self.assertEqual(state["state"], name)
                self.assertEqual(state["again"], {"lost": 2})
                self.assertEqual(imported(job, once=True)["again"], {"lost": 2, "once": True})
                self.assertEqual(imported(job, tries=15)["again"], {"lost": 10})
                for why, fields in (
                    ("a record of review", {"command": "review"}),
                    ("a record of the issue", {"target": 5}),
                ):
                    with self.subTest(f"{job} {why}"):
                        self.assertNotIn("again", imported(job, **fields))
        # Any other round is read as a review.
        self.assertEqual(imported(action="review", command="review")["again"], {"lost": 2})

    def test_an_issue_autopilot_does_not_take_gets_no_automatic_answers(self):
        given = json.loads((CASES / "go-answered-by-autopilot-runs-again.json").read_text())
        cfg = make_config(autopilot={**given["config"]["autopilot"], "scope": None})
        found = {}
        for number, user in ((42, "alice"), (43, "bob")):
            gh = FakeGitHub()
            gh.add_issue(number, user=user)
            gh.issue_comments[number] = comments_of(given["nexkit6"]["comments"], 1)
            state = first_state.import_item(gh, cfg, number)
            found[user] = (state["state"], state["attention"] and state["attention"]["reason"])
        # Only who opened the issue differs: autopilot does not take what a reader opened.
        taken, left = ("working", None), ("awaitingApproval", "question")
        self.assertEqual(found, {"alice": taken, "bob": left})

    def test_the_engine_refusing_a_blocked_go_imports_it_as_failed(self):
        given = json.loads((CASES / "go-needs-a-hand-made-change.json").read_text())
        refusal = mock.patch("nexkit.host.step", side_effect=host.Refused("no"))
        with refusal:
            kind, state = first_state.situation(**situation(given))
        self.assertEqual((kind, state["state"]), ("failed", "failed"))
        self.assertEqual(state["attention"]["reason"], "failed")

    def test_the_import_waits_for_the_open_issues_of_a_lower_wave(self):
        gh = FakeGitHub()
        gh.add_milestone(1)
        for number, label in ((40, "journey"), (41, "docs"), (42, "docs")):
            gh.add_issue(number, milestone=1, labels=(label,))
        labels = {"journey": {"wave": 1}, "docs": {"wave": 2}}
        cfg = make_config(autopilot={"enabled": True, "labels": labels})
        state = first_state.import_item(gh, cfg, 42)
        self.assertEqual(
            [(wait["for"], wait["on"]) for wait in state["waits"]],
            [("blocker", "40"), ("start", None)],
        )
        gh.issues[40]["state"] = "closed"
        state = first_state.import_item(gh, cfg, 42)
        self.assertEqual([wait["for"] for wait in state["waits"]], ["start"])

    def test_a_new_item_is_as_the_engine_makes_it(self):
        event = json.loads((protocol.ROOT / "examples" / "events" / "created.json").read_text())
        issue = {"number": 42, "title": event["title"]}
        state = first_state.imported(**situation({"nexkit6": {"issue": issue}}))
        made = json.loads(
            run_program(
                {
                    "protocol": 1,
                    "call": "step",
                    "state": None,
                    "event": {**event, "item": "42"},
                    "policies": {},
                }
            )
        )
        self.assertEqual(state, {**made["state"], "recentEvents": []})

    def test_a_part_three_levels_deep_is_imported_with_its_depth_and_is_not_split(self):
        gh = FakeGitHub()
        gh.add_issue(1)
        for child, parent in ((2, 1), (3, 2), (4, 3)):
            gh.add_issue(child, user=BOT_LOGIN, body=f"<!-- nexkit:part {parent} -->\nDo.")
            gh.parents[child] = parent
        run = {"command": "plan", "status": "running", "url": "https://x/actions/runs/7"}
        gh.comment(4, render_run(run, "Planning."))
        cfg = make_config()
        self.assertNotIn("depth", first_state.import_item(gh, cfg, 1))
        self.assertEqual(first_state.import_item(gh, cfg, 2)["depth"], 1)
        state = first_state.import_item(gh, cfg, 4)
        self.assertEqual((state["state"], state["depth"]), ("planning", 3))
        result = "@agent:planSplit"
        event = {"kind": "actionFinished", "action": "runAgent", "result": result}
        request = {
            "protocol": 1,
            "call": "step",
            "state": state,
            "event": {**event_of(event, state, 1), "item": state["item"]},
            "policies": {"planApproval": "autoWhenClean", "splits": True},
        }
        answer = json.loads(run_program(request))
        self.assertEqual(answer["state"]["state"], "awaitingApproval")
        self.assertEqual(answer["state"]["attention"]["reason"], "splitDepth")

    def part_with_a_timed_out_go(self, parent_asked, autopilot):
        """A part (#2) whose `/nexkit go` ran out of time, under a parent (#1) whose autopilot
        record says it asked for a split when `parent_asked`. The record of the parent is
        `autopilot` is the configuration of autopilot, or None to leave it off."""
        gh = FakeGitHub()
        gh.add_issue(1)
        gh.add_issue(2, user=BOT_LOGIN, body="<!-- nexkit:part 1 -->\nDo.")
        gh.parents[2] = 1
        if parent_asked:
            gh.comment(1, f"Autopilot.\n\n{record_marker({'split_asked': True, 'at': NOW})}")
        record = {"questions": [], "too_large": False}
        plan = {"plan": {"criteria": ["It is done."], "record": record}}
        gh.comment(2, body_of(plan))
        for number, command, status in ((7, "plan", "success"), (8, "go", "failure")):
            run = {"command": command, "status": status, "url": f"https://x/actions/runs/{number}"}
            if command == "go":
                run.update(outcome="agent_error", timed_out=True, attention="Out of time.")
            gh.comment(2, render_run(run, "The run."))
        return gh, make_config(**({"autopilot": autopilot} if autopilot else {}))

    def test_a_split_an_issue_above_asked_for_is_in_asked_and_a_time_out_fails(self):
        gh, cfg = self.part_with_a_timed_out_go(True, {"enabled": True, "scope": None})
        state = first_state.import_item(gh, cfg, 2)
        self.assertEqual(state["asked"], ["split"])
        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["attention"]["reason"], "failed")
        # Without the parent's record the same round asks for a split.
        gh, cfg = self.part_with_a_timed_out_go(False, {"enabled": True, "scope": None})
        state = first_state.import_item(gh, cfg, 2)
        self.assertEqual((state["state"], state["asked"]), ("planning", ["split"]))
        self.assertEqual(state["recentEvents"], ["run-8"])
        self.assertEqual([wait["for"] for wait in state["waits"]], ["start"])

    def test_a_split_asked_above_is_kept_for_an_issue_autopilot_does_not_take(self):
        gh, cfg = self.part_with_a_timed_out_go(True, None)
        state = first_state.import_item(gh, cfg, 2)
        self.assertEqual((state["state"], state["asked"]), ("failed", ["split"]))
        self.assertEqual(state["recentEvents"], [])
        gh, cfg = self.part_with_a_timed_out_go(False, None)
        state = first_state.import_item(gh, cfg, 2)
        self.assertEqual((state["state"], state["asked"]), ("failed", []))
        self.assertEqual(state["attention"]["reason"], "failed")

    def test_a_fix_round_that_stopped_as_blocked_is_answered_where_the_engine_puts_it(self):
        question = {"question": "Keep it?", "options": ["Yes", "No"], "recommended": "Yes"}
        row = {
            "round": 2,
            "trigger": "fix",
            "action": "fix",
            "status": "failure",
            "outcome": "agent_blocked",
            "head": "a" * 40,
            "url": "https://x/actions/runs/9",
            "questions": [question],
        }
        given = {
            "issue": {"number": 42},
            "pull": {"number": 308, "head": "a" * 40},
            "pullComments": [{"run": row}],
        }
        for answer, name, asked, events in (
            ("recommended", "fixing", ["answers"], ["run-9"]),
            ("never", "awaitingApproval", [], ["run-9"]),
        ):
            with self.subTest(answer):
                cfg = {"autopilot": {"enabled": True, "answer": answer}}
                case = {"nexkit6": given, "config": cfg}
                kind, state = first_state.situation(**situation(case))
                self.assertEqual((state["state"], state["asked"]), (name, asked))
                self.assertEqual(state["recentEvents"], events)
                if name == "fixing":
                    self.assertEqual([wait["for"] for wait in state["waits"]], ["start"])
                    self.assertEqual(state["pending"], [])
        # An issue that autopilot does not take keeps asking.
        state = first_state.imported(**situation({"nexkit6": given}))
        self.assertEqual((state["state"], state["recentEvents"]), ("awaitingApproval", []))

    def test_a_fix_round_that_ran_out_of_time_asks_for_a_split_and_a_withdrawal(self):
        row = {
            "round": 2,
            "trigger": "fix",
            "action": "fix",
            "status": "failure",
            "outcome": "agent_error",
            "timed_out": True,
            "head": "a" * 40,
            "url": "https://x/actions/runs/9",
        }
        given = {
            "issue": {"number": 42},
            "pull": {"number": 308, "head": "a" * 40},
            "pullComments": [{"run": row}],
        }
        case = {"nexkit6": given, "config": {"autopilot": {"enabled": True}}}
        kind, state = first_state.situation(**situation(case))
        self.assertEqual((kind, state["state"]), ("split asked", "planning"))
        self.assertEqual(state["asked"], ["split", "supersede"])
        self.assertEqual(state["recentEvents"], ["run-9"])
        state = first_state.imported(**situation({"nexkit6": given}))
        self.assertEqual((state["state"], state["asked"]), ("failed", []))

    def test_the_engine_s_answers_are_kept_only_for_the_run_they_started(self):
        plan = {"plan": {"record": {"questions": [], "too_large": False}}}
        ran = {"command": "plan", "status": "success", "outcome": "planned"}
        answers = {"answers": {"answers": [{"question": "Read again?", "answer": "Yes"}]}}

        def run(number, **fields):
            return {"run": {**ran, "url": f"https://x/actions/runs/{number}", **fields}}

        on_its_way = run(9, status="running", outcome=None)
        failed = run(8, status="failure", outcome="failed")
        quiet = run(8, outcome="retry")
        for name, after, asked, where in (
            ("a plan on its way", [on_its_way], ["answers"], "planning"),
            ("a run that changed nothing", [quiet, on_its_way], ["answers"], "planning"),
            ("a plan that reported", [run(8), on_its_way], [], "planning"),
            ("a plan that failed", [failed, on_its_way], [], "planning"),
            ("a person's comment", [{"person": "Wait."}, on_its_way], [], "planning"),
            ("a plan that waits", [], [], "awaitingApproval"),
        ):
            with self.subTest(name):
                given = {"issue": {"number": 42}, "comments": [plan, run(7), answers, *after]}
                state = first_state.imported(**situation({"nexkit6": given}))
                self.assertEqual((state["state"], state["asked"]), (where, asked))

    def test_a_run_on_its_way_is_the_action_the_engine_would_ask_for(self):
        given = {
            "issue": {"number": 42},
            "comments": [
                {"run": {"command": "plan", "status": "running", "url": "https://x/actions/runs/7"}}
            ],
        }
        state = first_state.imported(**situation({"nexkit6": given}))
        new = json.loads((protocol.ROOT / "examples" / "state" / "new.json").read_text())
        event = {
            "protocol": 1,
            "kind": "requested",
            "id": "e1",
            "item": new["item"],
            "at": NOW,
            "what": "plan",
            "by": {"id": "octocat", "role": "decides"},
            "note": "",
            "plan": None,
        }
        answer = json.loads(
            run_program(
                {"protocol": 1, "call": "step", "state": new, "event": event, "policies": {}}
            )
        )
        made = copy.deepcopy(answer["actions"][0])
        # The import does not know who started the run, so it leaves out the mark of a person's.
        made.pop("by", None)
        self.assertEqual(state["pending"], [{**made, "id": first_state.RUN_ID}])

    def test_the_import_counts_the_updates_of_the_branch_apart_from_conflict_rounds(self):
        def row(number, **fields):
            run = {
                "round": number,
                "trigger": "review",
                "status": "success",
                "url": f"https://x/{number}",
            }
            return {"run": {**run, **fields}}

        rows = [row(1), row(2, updated_branch=True), row(3, updated_branch=True)]
        rows.append(row(4, conflicts=True, base_sha="e" * 40))
        given = {
            "issue": {"number": 42},
            "pull": {"number": 308, "head": "a" * 40},
            "pullComments": rows,
        }
        state = first_state.imported(**situation({"nexkit6": given}))
        self.assertEqual((state["rounds"]["updates"], state["rounds"]["conflicts"]), (2, 1))
        given["pullComments"] = rows[:1]
        state = first_state.imported(**situation({"nexkit6": given}))
        self.assertNotIn("updates", state["rounds"])

    def test_the_fix_records_and_the_last_review_are_those_of_nexkit_6_s_marker(self):
        fixes = [
            {
                "round": 4,
                "head": "a" * 40,
                "auto": False,
                "summary": "Kept the header.",
                "note": "Keep the header",
                "failing": [{"name": "test", "signature": "fail test_x"}],
            },
            {
                "round": 6,
                "head": "b" * 40,
                "auto": True,
                "summary": "Read the stale fake again.",
                "note": "",
                "cause": "The test read a stale fake issue.",
                "diagnosis": True,
                "failing": [],
                "conflicts": ["a.py", "b.py"],
            },
        ]
        review = {
            "round": 7,
            "head": "b" * 40,
            "verdict": "request_changes",
            "findings": [
                {"severity": "suggestion", "file": "a.py", "line": 3, "body": "Name it."},
                {"severity": "blocking", "file": "", "line": 0, "body": "The close is lost."},
            ],
            "left_out": 2,
        }
        marker = {"version": 2, "issue": 42, "auto_fixes": 1, "feedback": None}
        given = {
            "issue": {"number": 42},
            "pull": {"number": 308, "head": "b" * 40},
            "pullComments": [{"state": {**marker, "fixes": fixes, "last_review": review}}],
        }
        rounds = first_state.imported(**situation({"nexkit6": given}))["rounds"]
        self.assertEqual(
            rounds["fixes"],
            [
                {
                    "round": 4,
                    "auto": False,
                    "diagnosis": False,
                    "note": "Keep the header",
                    "summary": "Kept the header.",
                    "cause": "",
                    "failing": [{"name": "test", "signature": "fail test_x"}],
                },
                {
                    "round": 6,
                    "auto": True,
                    "diagnosis": True,
                    "note": "",
                    "summary": "Read the stale fake again.",
                    "cause": "The test read a stale fake issue.",
                    "failing": [],
                    "conflicts": ["a.py", "b.py"],
                },
            ],
        )
        self.assertEqual(
            rounds["lastReview"],
            {
                "round": 7,
                "version": "b" * 40,
                "verdict": "request_changes",
                "findings": [
                    {"severity": "suggestion", "file": "a.py", "line": 3, "body": "Name it."},
                    {"severity": "blocking", "file": "", "line": 0, "body": "The close is lost."},
                ],
                "leftOut": 2,
            },
        )
        # The prompts clip the texts as the state keeps them, and a record that names no
        # round, or a review with no verdict the protocol knows, is not imported.
        long = {**fixes[0], "note": "n" * 5000, "summary": "s" * 9000, "cause": "c" * 5000}
        sig = {"name": "t" * 90, "signature": "g" * 900}
        found = {**review, "findings": [{**review["findings"][0], "body": "b" * 4000}]}
        given["pullComments"] = [
            {
                "state": {
                    **marker,
                    "fixes": [{**long, "failing": [sig]}, {"summary": "No round."}, "text"],
                    "last_review": found,
                }
            }
        ]
        rounds = first_state.imported(**situation({"nexkit6": given}))["rounds"]
        [fix] = rounds["fixes"]
        self.assertEqual(
            (len(fix["note"]), len(fix["summary"]), len(fix["cause"])), (2000 + 12, 4000 + 12, 2012)
        )
        self.assertEqual(
            (fix["failing"][0]["name"], len(fix["failing"][0]["signature"])),
            ("t" * 40 + "...", 200 + 12),
        )
        self.assertEqual(len(rounds["lastReview"]["findings"][0]["body"]), 1500 + 12)
        given["pullComments"] = [
            {"state": {**marker, "last_review": {**review, "verdict": "comment"}}}
        ]
        rounds = first_state.imported(**situation({"nexkit6": given}))["rounds"]
        self.assertNotIn("lastReview", rounds)
        self.assertNotIn("fixes", rounds)

    def test_the_state_for_a_prompt_ends_with_this_run_s_fix_numbered_after_the_review(self):
        fix = {"round": 4, "head": "a" * 40, "auto": False, "summary": "Kept.", "note": "n"}
        review = {"round": 6, "head": "b" * 40, "verdict": "approve", "findings": []}
        record = {"fixes": [fix], "last_review": review}
        change = {"id": "308", "version": "b" * 40}
        this = {"auto": True, "summary": "Merged.", "note": "", "cause": "Stale."}
        state = first_state.for_prompt(42, change, record, this)
        self.assertEqual(protocol.check(state, "state.schema.json"), [])
        self.assertEqual((state["item"], state["change"]), ("42", change))
        self.assertEqual([f["round"] for f in state["rounds"]["fixes"]], [4, 7])
        self.assertEqual(state["rounds"]["fixes"][1]["summary"], "Merged.")
        self.assertEqual(state["rounds"]["lastReview"]["version"], "b" * 40)
        # Without a fix, or without a marker, the history is the marker's.
        state = first_state.for_prompt(42, change, record)
        self.assertEqual([f["round"] for f in state["rounds"]["fixes"]], [4])
        state = first_state.for_prompt(42, None, None, this)
        self.assertEqual([f["round"] for f in state["rounds"]["fixes"]], [1])
        self.assertNotIn("lastReview", state["rounds"])

    def test_the_import_keeps_lists_and_signatures_within_the_state_s_bounds(self):
        part = {"title": "A part", "body": "Part.", "depends_on": []}
        record = {"questions": [], "too_large": True, "parts": 12, "split": [part] * 12}
        run = {"command": "plan", "status": "success", "outcome": "planned", "url": "https://x/1"}
        given = {"issue": {"number": 42}, "comments": [{"plan": {"record": record}}, {"run": run}]}
        state = first_state.imported(**situation({"nexkit6": given}))
        self.assertEqual((state["plan"]["parts"], state["plan"]["split"]), (12, []))
        parts = [{"number": 100 + n, "state": "open"} for n in range(12)]
        given = {"issue": {"number": 42}, "parts": parts}
        state = first_state.imported(**situation({"nexkit6": given}))
        self.assertEqual(len(state["parts"]), 10)
        check = {"name": "test", "passed": False, "signature": "x" * 10_000}
        feedback = {"head": "a" * 40, "checks": [check]}
        given = {
            "issue": {"number": 42},
            "pull": {"number": 308, "head": "a" * 40},
            "pullComments": [
                {"state": {"version": 2, "issue": 42, "auto_fixes": 0, "feedback": feedback}}
            ],
        }
        state = first_state.imported(**situation({"nexkit6": given}))
        self.assertTrue(state["rounds"]["signatures"][0].startswith("sha256:"))


if __name__ == "__main__":
    unittest.main()
