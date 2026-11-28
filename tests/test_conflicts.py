"""Automatic rounds that merge the base branch into NexKit pull requests."""

import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from nexkit import agent, autopilot, cli, first_state, host, item_state, progress
from nexkit.github import GitHubError, run_url
from nexkit.maintain import is_conflicting, maintain, resolve_conflicts, start_conflict_round
from nexkit.report import _not_up_to_date
from nexkit.route import route, skip_unaffected
from nexkit.state import (
    approved_head,
    check_fingerprint,
    read_state,
    render_run,
    render_state,
    setup_fingerprint,
)
from nexkit.usage import iso
from tests.support import (
    BOT_LOGIN,
    CALC,
    FIX_OUTPUT,
    IMPLEMENT_OUTPUT,
    SUB,
    FakeGitHub,
    GitRepos,
    git,
    make_config,
    play_sweep,
    store_fields,
    store_pending,
)
from tests.test_host import restate
from tests.test_report import HEAD, ReportCase, decision
from tests.test_route import comment_event

BASE_SHA = "f" * 40


def rounds_started(gh):
    """The rounds dispatched, without the sweep that the end of a run starts."""
    return [d for d in gh.dispatches if d["inputs"]["command"] != "autopilot"]


class SweepTests(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        for number, issue in ((6, 5), (7, 4), (8, 3)):
            self.gh.add_issue(issue)
            self.gh.add_pull(number, issue)
        self.gh.pulls[6]["mergeable"] = False
        self.gh.add_pull(9, 2, branch="feature/x")
        self.gh.pulls[9]["mergeable"] = False

    def sweep(self, **kw):
        return resolve_conflicts(self.gh, "main", "nexkit.yml", "main", 3, delay=0, **kw)

    def round(self, number, n, **run):
        row = {"round": n, "trigger": "fix", "url": f"https://x/{number}/{n}", "status": "success"}
        self.gh.comment(number, render_run({**row, **run}))

    def test_the_engine_answers_the_round_a_sweep_starts(self):
        with mock.patch.object(host, "base_moved", return_value="waits") as moved:
            self.sweep()
        self.assertEqual(self.gh.dispatches, [])
        # What the host asks: the pull request conflicts, and no round runs its checks.
        [(_, cfg, issue, number, head, conflicts, behind)] = [
            call.args for call in moved.call_args_list
        ]
        self.assertEqual((issue, number, conflicts, behind), (5, 6, True, False))
        self.assertEqual(cfg["conflicts"], {"auto_resolve": True, "max_rounds": 3})

    def test_a_sweep_starts_nothing_where_the_engine_refuses(self):
        with mock.patch.object(host, "base_moved", side_effect=host.Refused("no")):
            [line] = self.sweep()
        self.assertIn("#6: ", line)
        self.assertIn("refused to decide its next step", line)
        self.assertEqual(self.gh.dispatches, [])

    def test_the_conflict_rounds_used_are_the_stored_ones(self):
        # The sweep passes no count: the engine takes the conflict rounds used from the item's
        # stored `rounds`, and a pull request at the limit gets no conflict round.
        host.store_import(self.gh, make_config(), 5)
        restate(self.gh, 5, rounds={**item_state.read(self.gh, 5)[1]["rounds"], "conflicts": 3})
        [line] = self.sweep()
        self.assertIn("automatic conflict rounds are used up", line)
        self.assertEqual(self.gh.dispatches, [])

    def test_a_round_the_engine_leaves_out_says_the_limit_is_used_up_only_when_it_is(self):
        host.store_import(self.gh, make_config(), 5)
        found = {"left": False}
        line = start_conflict_round(self.gh, 6, "main", "nexkit.yml", "main", 3, **found)
        self.assertIn("so a person decides", line)
        self.assertNotIn("used up", line)
        restate(self.gh, 5, rounds={**item_state.read(self.gh, 5)[1]["rounds"], "conflicts": 3})
        line = start_conflict_round(self.gh, 6, "main", "nexkit.yml", "main", 3, **found)
        self.assertIn("automatic conflict rounds are used up", line)
        self.assertEqual(self.gh.dispatches, [])

    def test_starts_a_round_only_for_conflicting_nexkit_pull_requests(self):
        self.assertEqual(self.sweep(), ["Started an automatic round on #6 to merge `main`."])
        [dispatch] = self.gh.dispatches
        self.assertEqual(dispatch["inputs"]["auto"], "conflicts")
        self.assertEqual(dispatch["inputs"]["number"], "6")
        self.assertIn("Merge `main` into this branch", dispatch["inputs"]["note"])
        self.assertEqual(self.sweep(skip={6}), [])

    def test_the_same_base_commit_is_tried_once(self):
        self.round(6, 1, conflicts=True, base_sha=BASE_SHA, status="failure")
        self.assertEqual(self.sweep(), [])
        self.gh.branches["main"] = "e" * 40
        self.assertEqual(len(self.sweep()), 1)

    def test_rounds_are_limited_per_pull_request(self):
        for n in range(1, 4):
            self.round(6, n, conflicts=True, base_sha=str(n) * 40)
        self.round(6, 9, conflicts=True, resumed=True, base_sha="9" * 40)  # not counted twice
        [line] = self.sweep()
        self.assertIn("automatic conflict rounds are used up", line)
        self.assertFalse(self.gh.dispatches)

    def test_a_round_held_up_before_the_app_is_not_counted(self):
        for n in range(1, 3):
            self.round(6, n, conflicts=True, base_sha=str(n) * 40)
        self.round(6, 3, conflicts=True, base_sha="3" * 40, outcome="needs_merge")
        # Its token could not push workflow files; the App's can, so a round starts.
        self.assertEqual(self.sweep(), ["Started an automatic round on #6 to merge `main`."])

    def test_the_round_limit_is_configured(self):
        self.round(6, 1, conflicts=True, base_sha="1" * 40)
        [line] = resolve_conflicts(self.gh, "main", "nexkit.yml", "main", 1, delay=0)
        self.assertIn("its 1 automatic conflict rounds are used up", line)

    def test_a_paused_or_running_round_is_not_replaced(self):
        self.round(6, 1, status="paused")
        self.assertEqual(self.sweep(), [])
        self.round(6, 2, status="running")
        self.assertEqual(self.sweep(), [])
        self.round(6, 3, status="continued")  # its command runs again
        self.assertEqual(self.sweep(), [])
        self.assertFalse(self.gh.dispatches)

    def test_a_pull_request_whose_implement_round_runs_gets_no_conflict_round(self):
        go = {"command": "go", "url": "https://github.com/acme/app/actions/runs/40"}
        self.gh.comment(5, render_run({**go, "status": "running"}))
        self.gh.runs[40] = {"status": "in_progress"}
        self.assertEqual(self.sweep(), [])
        self.assertFalse(self.gh.dispatches)
        # A run that ended without a report stays on its way in the stored state until the
        # sweep's lost-run detection stores its end: the base branch's move waits for it.
        self.gh.runs[40] = {"status": "completed"}
        self.sweep()
        self.assertFalse(self.gh.dispatches)

    def test_unknown_mergeability(self):
        self.gh.pulls[6]["mergeable"] = None
        self.assertIsNone(is_conflicting(self.gh, 6, tries=2, delay=0))
        self.assertEqual(self.sweep(), ["GitHub has not worked out yet whether #6 conflicts."])

    def swept(self, **cfg):
        """One sweep with `conflicts.auto_resolve` and autopilot off. Returns the rounds it
        started."""
        cfg = make_config(conflicts={"auto_resolve": True}, **cfg)
        before = len(self.gh.dispatches)
        with mock.patch("nexkit.maintain.time.sleep"):
            maintain(self.gh, {"task": "sweep"}, cfg, workflow="nexkit.yml", ref="main")
        return [d["inputs"]["number"] for d in self.gh.dispatches[before:]]

    def test_a_sweep_starts_the_round_of_a_conflicting_pull_request_once(self):
        # `main` moved while a fix round ran, which held the conflict round
        # back; the round then ended. Nothing started it until a person did.
        self.round(6, 1, status="running")
        self.assertEqual(self.swept(), [])
        self.round(6, 2, status="success", outcome="ready")
        self.assertEqual(self.swept(), ["6"])
        # The start is recorded on the latest round, so the next sweep, before the
        # conflict round posts its comment, does not start it twice.
        self.assertEqual(self.gh.run_comments(6)[-1]["run"]["conflicts_started"], BASE_SHA)
        self.assertEqual(self.swept(), [])
        # Its comment says it runs, and then that it is blocked on this base commit.
        self.round(6, 3, status="running", conflicts=True, base_sha=BASE_SHA)
        self.assertEqual(self.swept(), [])
        self.gh.issue_comments[6][-1]["body"] = render_run(
            {**self.gh.run_comments(6)[-1]["run"], "status": "success", "outcome": "ready"}
        )
        self.assertEqual(self.swept(), [])
        # The base branch moves: a new round.
        self.gh.branches["main"] = "e" * 40
        self.assertEqual(self.swept(), ["6"])

    def test_a_failed_round_gets_no_conflict_round_from_a_sweep(self):
        # The engine's rule, which NexKit 6 did not have: an item whose round failed waits for
        # a person, whatever the base branch does.
        self.round(6, 1, status="failure", outcome="agent_error")
        self.assertEqual(self.swept(), [])
        self.gh.branches["main"] = "e" * 40
        self.assertEqual(self.swept(), [])
        self.assertEqual(self.gh.dispatches, [])

    def test_a_round_that_ended_without_a_report_starts_no_conflict_round_of_the_sweep(self):
        # The fix round's runner was lost: its `finish` job starts a sweep, which with
        # autopilot off ends the round's comment itself. The stored state still holds the
        # round on its way, so the engine starts no conflict round for the base branch's move:
        # a lost round is the lost-run detection's, not the move's.
        url = "https://github.com/acme/app/actions/runs/40"
        self.round(6, 1, status="running", url=url, head="b" * 40)
        self.gh.runs[40] = {"status": "in_progress"}
        self.assertEqual(self.swept(), [])
        self.gh.runs[40] = {"status": "completed", "conclusion": "failure"}
        self.assertEqual(self.swept(), [])
        run = self.gh.run_comments(6)[-1]["run"]
        self.assertEqual((run["status"], run["outcome"]), ("failure", "lost"))
        self.assertNotIn("conflicts_started", run)
        self.assertEqual({s["state"] for s in self.gh.statuses}, {"error"})
        # With autopilot, its sweep handles such a round first, and may start it once more.
        self.round(6, 2, status="running", url=url)
        with mock.patch("nexkit.autopilot.sweep", return_value=[]):
            self.assertEqual(self.swept(autopilot={"enabled": True}), [])
        self.assertEqual(self.gh.run_comments(6)[-1]["run"]["status"], "running")

    def test_a_round_that_started_the_next_one_holds_the_sweep_back(self):
        # An automatic fix round is on its way and merges the base branch itself; a
        # dispatch now would replace it in the queue.
        self.round(6, 1, outcome="auto_fix")
        self.gh.issue_comments[6][-1]["updated_at"] = iso(datetime.now(UTC))
        self.assertEqual(self.swept(), [])
        # One that did not run within hours stays on its way in the stored state: the sweep's
        # lost-run detection ends it, and the base branch's move waits for that.
        self.gh.issue_comments[6][-1]["updated_at"] = "2020-01-01T00:00:00Z"
        self.assertEqual(self.swept(), [])

    def test_a_round_after_the_tried_one_lets_the_same_base_commit_be_tried_again(self):
        self.round(6, 1, conflicts=True, base_sha=BASE_SHA, status="failure")
        self.assertEqual(self.swept(), [])
        self.round(6, 2, trigger="review", status="success", outcome="ready")
        self.assertEqual(self.swept(), ["6"])

    def test_unknown_mergeability_waits_for_the_next_sweep(self):
        self.gh.pulls[6]["mergeable"] = None
        self.assertEqual(self.swept(), [])
        self.gh.pulls[6]["mergeable"] = False
        self.assertEqual(self.swept(), ["6"])

    def test_a_sweep_leaves_alone_a_pull_request_autopilot_started_a_command_on(self):
        busy = {6}
        with mock.patch(
            "nexkit.autopilot.sweep", side_effect=lambda *a, **_: a[-1].update(busy) or []
        ):
            self.assertEqual(self.swept(autopilot={"enabled": True}), [])
        with mock.patch("nexkit.autopilot.sweep", return_value=["Autopilot: nothing."]):
            self.assertEqual(self.swept(autopilot={"enabled": True}), ["6"])

    def test_maintain_tasks(self):
        cfg = make_config(conflicts={"auto_resolve": True})
        with mock.patch("nexkit.maintain.time.sleep"):
            maintain(
                self.gh, {"task": "conflicts", "base": "main"}, cfg, workflow="n.yml", ref="main"
            )
            self.assertEqual(len(self.gh.dispatches), 1)
            self.gh.branches["main"] = "e" * 40
            maintain(self.gh, {"task": "schedule"}, cfg, workflow="n.yml", ref="main")
            self.assertEqual(len(self.gh.dispatches), 2)
            maintain(self.gh, {"task": "schedule"}, make_config(), workflow="n.yml", ref="main")
            self.assertEqual(len(self.gh.dispatches), 2)


NEW_BASE = "e" * 40
CHECKED = "d" * 40


class SupersededTests(unittest.TestCase):
    """A round still in its checks when the base branch moves under it."""

    def setUp(self):
        self.cfg = make_config(setup=["npm ci"])
        self.gh = FakeGitHub()
        self.gh.add_issue(5)
        self.gh.add_pull(6, 5, head_sha=CHECKED)
        self.gh.comment(6, render_state({"version": 2, "issue": 5, "auto_fixes": 1}))
        first = {"round": 1, "trigger": "implement", "status": "success", "outcome": "auto_fix"}
        self.gh.comment(6, render_run({**first, "url": "https://x/runs/1", "head": "a" * 40}))
        self.fix = {
            "round": 2,
            "trigger": "fix (auto)",
            "status": "running",
            "url": "https://github.com/acme/app/actions/runs/50",
            "head": "a" * 40,
            "setup": setup_fingerprint(self.cfg["setup"]),
        }
        self.gh.comment(6, render_run(self.fix))
        self.in_job(50, "verify")
        # `main` moved since the round published `CHECKED`.
        self.gh.branches["main"] = NEW_BASE

    def in_job(self, run_id, stage):
        """Run `run_id` is in `stage`: the jobs before it ended, and it runs."""
        order = ["route", "agent", "publish", "shards", "verify", "combine", "review", "report"]
        jobs = [{"name": f"nexkit / {job}", "status": "completed"} for job in order]
        jobs = jobs[: order.index(stage)]
        if stage == "verify":
            # Of two check jobs, one ended.
            jobs.append({"name": "nexkit / verify (unit)", "status": "completed"})
            jobs.append({"name": "nexkit / verify (e2e)", "status": "in_progress"})
        else:
            jobs.append({"name": f"nexkit / {stage}", "status": "in_progress"})
        self.gh.jobs[run_id] = jobs
        self.gh.runs[run_id] = {"status": "in_progress", "run_attempt": 1}

    def behind(self):
        """The rules want pull requests up to date, and the head lacks `main`'s tip."""
        self.gh.strict = True
        self.gh.compares[(NEW_BASE, CHECKED)] = {"behind_by": 1}

    def sweep(self, max_rounds=3):
        with mock.patch("nexkit.maintain.time.sleep"):
            return resolve_conflicts(self.gh, "main", "nexkit.yml", "main", max_rounds, delay=0)

    def latest(self, number=6):
        return self.gh.run_comments(number)[-1]

    def next_round(self):
        """The decision of the automatic review that the stop started, as `nexkit route`
        gives it once it read the configuration."""
        # The stop stored the checks of the review that follows it as pending, which the
        # dispatch carries out.
        host.store_import(self.gh, self.cfg, 5)
        comment, held = item_state.read(self.gh, 5)
        carried = [a for a in held["pending"] if a["kind"] == "runChecks"]
        if not carried or carried[0]["id"] == first_state.RUN_ID:
            checks = {"id": "a2.1", "next": None, "kind": "runChecks", "changeVersion": "a" * 40}
            asked = {**held, "revision": held["revision"] + 1, "state": "checking"}
            asked["pending"] = [checks]
            self.assertTrue(item_state.write(self.gh, 5, asked, (comment, held)))
        inputs = {"command": "review", "number": "6", "auto": "true"}
        decision = route(
            self.gh, "workflow_dispatch", {"sender": {"login": "x"}}, inputs, cfg=self.cfg
        )
        return skip_unaffected(self.gh, decision, self.cfg)

    def test_a_round_checking_a_head_now_behind_stops_and_the_branch_is_updated(self):
        self.behind()
        [line] = self.sweep()
        self.assertIn("NexKit stopped this round's checks: `main` moved to `eeeeeee`", line)
        self.assertEqual(self.gh.cancelled, [50])
        self.assertEqual(self.gh.updated, [(6, CHECKED)])
        [review] = rounds_started(self.gh)
        self.assertEqual(
            (review["inputs"]["command"], review["inputs"]["auto"]), ("review", "true,carry")
        )
        row = self.latest()
        run = row["run"]
        self.assertEqual((run["status"], run["outcome"]), ("success", "superseded"))
        self.assertEqual(
            (run["head"], run["checks"], run["verdict"]), (CHECKED, "not run", "not run")
        )
        self.assertTrue(run["updated_branch"])
        self.assertIn("want pull requests up to date, and `ddddddd` lacks it", row["body"])
        self.assertIn("started `/nexkit review` on the new commit", row["body"])
        # Not a round a person cancelled, nor one of the automatic fix rounds.
        self.assertEqual(read_state(self.gh.comments(6))[1]["auto_fixes"], 0)
        stopped = {(s["sha"], s["state"]) for s in self.gh.statuses}
        self.assertEqual(stopped, {(CHECKED, "error"), ("a" * 40, "error")})
        # Once: the next look finds no round in its checks.
        self.assertEqual(self.sweep(), [])
        self.assertEqual(len(rounds_started(self.gh)), 1)

    def test_the_engine_answers_the_stop_of_a_round_whose_head_is_behind(self):
        self.behind()
        with mock.patch.object(host, "base_moved", return_value="waits") as moved:
            self.assertEqual(self.sweep(), [])
        # Nothing stopped, nothing updated, nothing started.
        nothing = (self.gh.cancelled, self.gh.updated, rounds_started(self.gh))
        self.assertEqual(nothing, ([], [], []))
        self.assertEqual(self.latest()["run"]["status"], "running")
        # What the host asks: behind, with no conflict.
        [(_, _, issue, number, head, conflicts, behind)] = [
            call.args for call in moved.call_args_list
        ]
        self.assertEqual((issue, number, head), (5, 6, CHECKED))
        self.assertEqual((conflicts, behind), (False, True))

    def test_the_engine_answers_the_stop_of_a_round_whose_head_conflicts(self):
        self.gh.pulls[6]["mergeable"] = False
        with mock.patch.object(host, "base_moved", return_value="waits") as moved:
            self.assertEqual(self.sweep(), [])
        self.assertEqual((self.gh.cancelled, rounds_started(self.gh)), ([], []))
        # Asked for the round's stop first, then for the round that no round runs against.
        first, *_ = moved.call_args_list
        self.assertEqual(first.args[5:7], (True, False))

    def test_a_round_is_stopped_for_an_update_only_when_the_engine_answers_one(self):
        self.behind()
        with mock.patch.object(host, "base_moved", return_value="update"):
            [line] = self.sweep()
        self.assertIn("NexKit stopped this round's checks", line)
        self.assertEqual((self.gh.cancelled, self.gh.updated), ([50], [(6, CHECKED)]))

    def test_a_round_is_stopped_for_a_conflict_round_only_when_the_engine_answers_one(self):
        self.gh.pulls[6]["mergeable"] = False
        with mock.patch.object(host, "base_moved", return_value="round"):
            [line] = self.sweep()
        self.assertIn("NexKit stopped this round's checks", line)
        self.assertEqual(self.gh.cancelled, [50])
        [dispatch] = rounds_started(self.gh)
        self.assertEqual(dispatch["inputs"]["auto"], "conflicts")

    def test_a_round_whose_stop_the_engine_refuses_goes_on(self):
        self.behind()
        with mock.patch.object(host, "base_moved", side_effect=host.Refused("no")):
            [line] = self.sweep()
        self.assertIn("refused to decide its next step", line)
        nothing = (self.gh.cancelled, self.gh.updated, rounds_started(self.gh))
        self.assertEqual(nothing, ([], [], []))
        self.assertEqual(self.latest()["run"]["status"], "running")

    def test_a_round_counting_its_shards_is_in_its_checks_too(self):
        self.in_job(50, "shards")
        self.behind()
        [line] = self.sweep()
        self.assertIn("NexKit stopped this round's checks", line)
        self.assertEqual(self.gh.cancelled, [50])

    def part(self, artifact_id, name, results, head=CHECKED, run_id=50):
        """A check job's artifact of run `run_id`: its results and the commit it checked."""
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("checks.json", json.dumps(results))
            if head is not None:
                archive.writestr("checked.json", json.dumps({"head": head}))
            archive.writestr("results/e2e/log.txt", "...")
        self.gh.zips[artifact_id] = data.getvalue()
        artifacts = self.gh.run_artifact_list.setdefault(run_id, [])
        artifacts.append({"id": artifact_id, "name": name, "size_in_bytes": len(data.getvalue())})

    def test_a_stopped_round_records_the_checks_that_passed_on_its_head(self):
        def result(name, passed):
            return {"name": name, "run": f"make {name}", "passed": passed, "output": ""}

        # The finished job of group `unit`: `unit` passed, `types` failed.
        self.part(1, "nexkit-checks-unit", [result("unit", True), result("types", False)])
        # A job of the same run that checked another commit, one without the commit it
        # checked, and the round's joined results, which no stopped round has.
        self.part(2, "nexkit-checks-lint", [result("lint", True)], head="b" * 40)
        self.part(3, "nexkit-checks-docs", [result("docs", True)], head=None)
        self.part(4, "nexkit-checks", [result("e2e", True)])
        self.behind()
        [line] = self.sweep()
        self.assertIn("Before it stopped, `unit` passed on `ddddddd`.", line)
        run = self.latest()["run"]
        self.assertEqual(
            (run["outcome"], run["head"], run["checks"]), ("superseded", CHECKED, "not run")
        )
        self.assertEqual(run["passed"], {"unit": check_fingerprint("make unit")})
        self.assertEqual(run["setup"], self.fix["setup"])
        # The next round of the pull request gets it, when it runs the same setup.
        self.gh.pulls[6]["head"]["sha"] = "f" * 40
        self.assertEqual(
            self.next_round()["check_heads"], {"unit": {check_fingerprint("make unit"): CHECKED}}
        )
        self.cfg = make_config(setup=["pnpm i"])
        self.assertNotIn("check_heads", self.next_round())

    def test_a_stopped_round_without_finished_check_jobs_records_none(self):
        self.gh.run_artifact_list[50] = [{"id": 9, "name": "nexkit-checks-unit"}]
        self.gh.zips[9] = b"not a zip"
        self.behind()
        [line] = self.sweep()
        self.assertNotIn("Before it stopped", line)
        self.assertNotIn("passed", self.latest()["run"])
        self.assertNotIn("check_heads", self.next_round())

    def test_a_round_checking_a_head_that_now_conflicts_starts_the_conflict_round(self):
        self.gh.pulls[6]["mergeable"] = False
        [line] = self.sweep()
        self.assertIn("`ddddddd` conflicts with it", line)
        self.assertEqual(self.gh.cancelled, [50])
        [started] = rounds_started(self.gh)
        self.assertEqual(
            (started["inputs"]["auto"], started["inputs"]["number"]), ("conflicts", "6")
        )
        run = self.latest()["run"]
        self.assertEqual((run["outcome"], run["conflicts_started"]), ("superseded", NEW_BASE))
        self.assertIn("Started an automatic round on #6 to merge `main`.", self.latest()["body"])
        # Without up-to-date rules too: a head that conflicts never merges.
        self.assertFalse(self.gh.strict)
        self.assertEqual(self.sweep(), [])
        self.assertEqual(len(rounds_started(self.gh)), 1)

    def test_a_round_in_any_other_job_goes_on(self):
        self.behind()
        self.gh.pulls[6]["mergeable"] = False
        for stage in ("agent", "publish", "review", "report"):
            self.in_job(50, stage)
            self.assertEqual(self.sweep(), [], stage)
        self.gh.runs[50] = {"status": "queued"}
        self.assertEqual(self.sweep(), [])
        self.assertEqual((self.gh.cancelled, rounds_started(self.gh)), ([], []))
        self.assertEqual(self.latest()["run"]["status"], "running")

    def test_checks_that_can_still_lead_to_a_merge_go_on(self):
        # The head holds `main`'s tip.
        self.gh.strict = True
        self.gh.compares[(NEW_BASE, CHECKED)] = {"behind_by": 0}
        self.assertEqual(self.sweep(), [])
        # Or the rules do not want pull requests up to date.
        self.behind()
        self.gh.strict = False
        self.assertEqual(self.sweep(), [])
        self.assertEqual((self.gh.cancelled, rounds_started(self.gh)), ([], []))

    def test_a_round_whose_next_round_has_no_room_left_goes_on(self):
        self.behind()
        for n in (1, 2):
            self.gh.comment(
                6,
                render_run(
                    {
                        "round": 10 + n,
                        "trigger": "review",
                        "updated_branch": True,
                        "status": "success",
                        "url": f"https://x/runs/{n}",
                    }
                ),
            )
        self.gh.comment(6, render_run(self.fix))
        self.assertEqual(self.sweep(max_rounds=2), [])
        self.assertEqual(self.gh.cancelled, [])

    def test_the_implement_round_that_opened_the_pull_request_stops_too(self):
        gh = self.gh = FakeGitHub()
        gh.add_issue(5)
        gh.add_pull(6, 5, head_sha=CHECKED)
        gh.branches["main"] = NEW_BASE
        go = {
            "command": "go",
            "status": "running",
            "url": "https://github.com/acme/app/actions/runs/60",
        }
        gh.comment(5, render_run(go))
        self.in_job(60, "combine")
        # The round's publish job stored its commit, so the stored state shows the checks.
        host.store_import(gh, self.cfg, 5)
        restate(
            gh,
            5,
            state="checking",
            change={"id": "6", "version": CHECKED},
            pending=[{"id": "a2.1", "next": None, "kind": "runChecks", "changeVersion": CHECKED}],
        )
        self.behind()
        [line] = self.sweep()
        self.assertEqual(gh.cancelled, [60])
        self.assertEqual(gh.updated, [(6, CHECKED)])
        # The pull request shows the implement round as its first round.
        [row] = gh.run_comments(6)
        self.assertEqual(
            (row["run"]["round"], row["run"]["trigger"], row["run"]["outcome"]),
            (1, "implement", "superseded"),
        )
        issue_run = self.latest(5)
        self.assertEqual(
            (issue_run["run"]["status"], issue_run["run"]["outcome"]), ("success", "superseded")
        )
        self.assertIn("Pull request #6: NexKit stopped this round's checks", issue_run["body"])

    def test_a_next_round_that_cannot_start_waits_for_a_person(self):
        self.behind()
        with mock.patch.object(self.gh, "update_branch", side_effect=GitHubError(422, "no")):
            [line] = self.sweep()
        self.assertIn("NexKit could not update the branch", line)
        run = self.latest()["run"]
        self.assertEqual((run["status"], run["outcome"]), ("failure", "stop_failed"))
        self.assertIn("could not start the next round", run["attention"])

    def test_a_push_to_the_base_branch_stops_the_round(self):
        self.behind()
        cfg = make_config(conflicts={"auto_resolve": True})
        event = {"ref": "refs/heads/main", "repository": {"default_branch": "main"}}
        with mock.patch("nexkit.maintain.time.sleep"):
            maintain(self.gh, route(self.gh, "push", event), cfg, workflow="nexkit.yml", ref="main")
        self.assertEqual(self.gh.cancelled, [50])


class PushRunStoresNothingTest(unittest.TestCase):
    """The push run previews the conflict round and dispatches it: the run of the round counts
    it in the item's group, so the push run leaves the item's state comment as it is."""

    def test_the_push_run_leaves_the_state_comment_as_it_is(self):
        gh = FakeGitHub()
        gh.add_issue(5)
        gh.add_pull(6, 5)
        gh.pulls[6]["mergeable"] = False
        cfg = make_config(conflicts={"auto_resolve": True, "max_rounds": 2})
        self.assertTrue(host.store_import(gh, cfg, 5))
        comment, doc = item_state.read(gh, 5)
        body = comment["body"]
        event = {"ref": "refs/heads/main", "repository": {"default_branch": "main"}}
        task = route(gh, "push", event)
        self.assertEqual(task["task"], "conflicts")
        with mock.patch("nexkit.maintain.time.sleep"):
            maintain(gh, task, cfg, workflow="nexkit.yml", ref="main")
        [dispatched] = rounds_started(gh)
        self.assertEqual(dispatched["inputs"]["auto"], "conflicts")
        after, found = item_state.read(gh, 5)
        self.assertEqual((after["body"], found["revision"]), (body, doc["revision"]))
        self.assertEqual(found["rounds"]["conflicts"], 0)


class ReviewAfterMergeUpdateTest(unittest.TestCase):
    """A merge that GitHub refused as behind stores the update that it asks for (`accept_answer`),
    and the review that follows the update stores the update's result for that pending action."""

    NEW = "e" * 40

    def test_the_review_stores_the_result_of_the_pending_update_and_starts(self):
        gh = FakeGitHub()
        gh.add_issue(5)
        gh.add_pull(6, 5)
        cfg = make_config(conflicts={"auto_resolve": True, "max_rounds": 2})
        store_pending(gh, cfg, 5, 6, HEAD)
        refusal = {"accepted": False, "refusal": "behind", "text": "x"}
        answer = host.accept_answer(gh, cfg, 5, 6, refusal, write=True)
        self.assertEqual(answer[0], "update")
        found = item_state.read(gh, 5)[1]
        self.assertEqual([a["kind"] for a in found["pending"]], ["updateChange"])
        # The update moved the head, and its review is dispatched.
        gh.pulls[6]["head"]["sha"] = self.NEW
        inputs = {"command": "review", "number": "6", "note": "", "auto": "true,carry"}
        event = {"inputs": inputs, "sender": {"login": BOT_LOGIN}}
        with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": "2001"}):
            decision = route(gh, "workflow_dispatch", event, inputs, cfg=cfg)
        self.assertEqual(decision["action"], "review")
        after = item_state.read(gh, 5)[1]
        self.assertEqual(after["rounds"]["updates"], 1)
        self.assertEqual([a["kind"] for a in after["pending"]], ["runChecks"])
        self.assertEqual(after["pending"][0]["changeVersion"], self.NEW)
        self.assertEqual(after["change"]["version"], self.NEW)


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        self.gh.add_issue(5)
        self.gh.add_pull(6, 5)
        self.cfg = make_config()

    def test_pushes_to_the_default_branch(self):
        event = {"ref": "refs/heads/main", "repository": {"default_branch": "main"}}
        self.assertEqual(
            route(self.gh, "push", event),
            {"action": "maintain", "task": "conflicts", "base": "main"},
        )
        event["ref"] = "refs/heads/feature"
        self.assertEqual(route(self.gh, "push", event)["action"], "none")

    def test_a_persons_command_waits_while_the_implement_round_runs(self):
        go = {"command": "go", "url": "https://github.com/acme/app/actions/runs/40"}
        self.gh.comment(5, render_run({**go, "status": "running"}))
        self.gh.runs[40] = {"status": "in_progress"}
        for event_name, event in (
            ("issue_comment", comment_event(6, "/nexkit fix Tidy it", on_pull=True)),
            (
                "pull_request_review",
                {
                    "action": "submitted",
                    "pull_request": {"number": 6},
                    "review": {"state": "changes_requested", "user": {"login": "alice"}},
                },
            ),
        ):
            decision = route(self.gh, event_name, event, cfg=self.cfg)
            self.assertEqual((decision["action"], decision["reply_to"]), ("none", 6))
            self.assertIn(
                "[implement round](https://github.com/acme/app/actions/runs/40)", decision["reason"]
            )
            self.assertIn("one NexKit round at a time", decision["reason"])
        # The rounds that the implement round starts itself as it ends go on: its stored state
        # holds the checks that it asked for, which the dispatch carries out.
        self.assertTrue(host.store_import(self.gh, self.cfg, 5))
        comment, held = item_state.read(self.gh, 5)
        checks = {"id": "a2.1", "next": None, "kind": "runChecks", "changeVersion": "a" * 40}
        asked = {**held, "revision": 2, "state": "checking", "pending": [checks]}
        self.assertTrue(item_state.write(self.gh, 5, asked, (comment, held)))
        inputs = {"command": "review", "number": "6", "note": "", "auto": "retry"}
        dispatch = route(
            self.gh, "workflow_dispatch", {"sender": {"login": BOT_LOGIN}}, inputs, cfg=self.cfg
        )
        self.assertEqual(dispatch["action"], "review")
        # Once it ended, its report left the pull request ready for a person, and the command runs.
        self.gh.comment(5, render_run({**go, "status": "success"}))
        comment, held = item_state.read(self.gh, 5)
        ready = {**held, "revision": held["revision"] + 1, "state": "ready", "pending": []}
        self.assertTrue(item_state.write(self.gh, 5, ready, (comment, held)))
        fix = route(
            self.gh, "issue_comment", comment_event(6, "/nexkit fix", on_pull=True), cfg=self.cfg
        )
        self.assertEqual(fix["action"], "fix")

    def test_a_conflict_round_records_the_base_commit(self):
        self.gh.branches["main"] = "e" * 40
        inputs = {"command": "fix", "number": "6", "note": "Merge", "auto": "conflicts"}
        decision = route(
            self.gh, "workflow_dispatch", {"sender": {"login": BOT_LOGIN}}, inputs, cfg=self.cfg
        )
        self.assertEqual(decision["base_sha"], "e" * 40)
        self.assertTrue(decision["conflicts"])


class ConcurrentStartTests(unittest.TestCase):
    """A push to the default branch and a sweep look at a conflicting pull request at the
    same moment, before either records its start: both start a conflict round."""

    def setUp(self):
        self.gh = FakeGitHub()
        self.gh.add_issue(5)
        self.gh.add_pull(6, 5)
        self.gh.pulls[6]["mergeable"] = False
        self.cfg = make_config()
        row = {"round": 1, "trigger": "fix", "url": "https://x/6/1", "status": "success"}
        self.gh.comment(6, render_run(row))

    def test_the_second_conflict_round_started_at_the_same_time_runs_nothing(self):
        read, seen = self.gh.comments, {}

        def stale(number):
            # Each starter reads the comments before the other one writes its start.
            return copy.deepcopy(seen.setdefault(number, copy.deepcopy(read(number))))

        with mock.patch.object(self.gh, "comments", side_effect=stale):
            for _ in range(2):
                resolve_conflicts(self.gh, "main", "nexkit.yml", "main", 3, delay=0)
        started = [d["inputs"] for d in rounds_started(self.gh)]
        self.assertEqual(len(started), 2)
        # The first runs and ends on this base commit.
        sender = {"sender": {"login": BOT_LOGIN}}
        first = route(self.gh, "workflow_dispatch", sender, started[0], cfg=self.cfg)
        self.assertEqual(first["action"], "fix")
        row = {"round": 2, "url": "https://x/6/2", "status": "failure", "outcome": "agent_blocked"}
        row.update(trigger="fix (auto, conflicts)", conflicts=True, base_sha=first["base_sha"])
        self.gh.comment(6, render_run(row))
        # The second, which waited behind it in the pull request's group, runs nothing.
        second = route(self.gh, "workflow_dispatch", sender, started[1], cfg=self.cfg)
        self.assertEqual((second["action"], second["reply_to"]), ("none", None))
        self.assertIn("A conflict round already ran on `main`", second["reason"])
        # A round that resumes or retries it, or starts once more after it ended without a
        # report, goes on.
        for auto in ("conflicts,resume", "conflicts,retry"):
            again = route(
                self.gh, "workflow_dispatch", sender, {**started[1], "auto": auto}, cfg=self.cfg
            )
            self.assertEqual(again["action"], "fix", auto)
        # The stored state holds the round the reports and the resumes left, which the sweep's
        # lost-run detection ends; here that end is stored by hand.
        self.gh.issue_comments[6][-1]["body"] = render_run({**row, "outcome": "lost"})
        host.store_import(self.gh, self.cfg, 5)
        restate(self.gh, 5, state="ready", pending=[], waits=[], recentEvents=[])
        self.assertEqual(
            route(self.gh, "workflow_dispatch", sender, started[1], cfg=self.cfg)["action"], "fix"
        )
        # After the base branch moves, a new round goes on too.
        self.gh.issue_comments[6][-1]["body"] = render_run(row)
        host.store_import(self.gh, self.cfg, 5)
        restate(self.gh, 5, state="ready", pending=[], waits=[], recentEvents=[])
        self.gh.branches["main"] = "e" * 40
        self.assertEqual(
            route(self.gh, "workflow_dispatch", sender, started[1], cfg=self.cfg)["action"], "fix"
        )


MUL_TOP = "def mul(a, b):\n    return a * b\n\n\n"
DIV = "\n\ndef div(a, b):\n    return a / b\n"
DIV_TOP = "def div(a, b):\n    return a / b\n\n\n"


class UpToDateTests(unittest.TestCase):
    def setUp(self):
        self.repos = GitRepos()
        self.repo = self.repos.clone("work")
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()
        self.repos.cleanup()

    def test_conflicts_with_base(self):
        self.repos.diverge()
        git(self.repo, "fetch", "-q", "origin")
        git(self.repo, "checkout", "-q", "-B", "nexkit/issue-5", "origin/nexkit/issue-5")
        self.assertTrue(agent.conflicts_with_base(self.repo, "main"))
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "reset", "-q", "--hard", "origin/main")
        self.assertFalse(agent.conflicts_with_base(self.repo, "main"))

    def checkout(self, branch="nexkit/issue-5"):
        git(self.repo, "fetch", "-q", "origin")
        git(self.repo, "checkout", "-q", "-B", branch, f"origin/{branch}")

    def run_agent(self, **fields):
        (self.out / "context.json").write_text("{}")
        d = decision("fix", pr=6, target=6, head=HEAD, auto=True, **fields)
        env = {"NEXKIT_DECISION": json.dumps(d), "NEXKIT_CONFIG": json.dumps(make_config())}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
            cli.main(["agent", "--repo", str(self.repo), "--out", str(self.out)])
        return json.loads((self.out / "result.json").read_text())

    def test_a_conflict_round_without_a_conflict_runs_nothing(self):
        self.repos.push_commit("dev", "nexkit/issue-5", {"calc.py": CALC + SUB}, "Add sub")
        self.checkout()
        self.assertEqual(self.run_agent(conflicts=True)["status"], "up_to_date")

    def test_a_change_the_base_branch_already_holds_runs_nothing(self):
        self.repos.push_commit("dev", "nexkit/issue-5", {"calc.py": CALC + SUB}, "Add sub")
        self.repos.push_commit("dev", "main", {"README.md": "Calc\n"}, "Readme")
        # Another pull request makes the same change while it adds `mul` to the same file.
        brought = self.repos.push_commit("dev", "main", {"calc.py": MUL_TOP + CALC + SUB}, "Both")
        base = self.repos.push_commit(
            "dev", "main", {"calc.py": DIV_TOP + MUL_TOP + CALC + SUB}, "Add div"
        )
        self.checkout()
        result = self.run_agent()
        self.assertEqual(
            {key: result[key] for key in ("status", "merge_base", "on_base")},
            {"status": "already_on_base", "merge_base": base, "on_base": brought},
        )
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")  # nothing merged

    def test_a_change_of_its_own_goes_on(self):
        self.repos.push_commit("dev", "nexkit/issue-5", {"calc.py": CALC + SUB + DIV}, "Sub, div")
        both = self.repos.push_commit("dev", "main", {"calc.py": MUL_TOP + CALC + SUB}, "Both")
        self.checkout()
        self.assertIsNone(agent.on_base(self.repo, "main"))
        # Once its own commits drop what `main` lacks, `main` holds all of it.
        self.repos.push_commit("dev", "nexkit/issue-5", {"calc.py": CALC + SUB}, "Drop div")
        self.checkout()
        self.assertEqual(agent.on_base(self.repo, "main")["on_base"], both)


class ConflictReportTests(ReportCase):
    def test_a_round_whose_change_is_already_on_base_closes_and_gives_its_round_back(self):
        state = {"version": 2, "issue": 5, "auto_fixes": 1, "feedback": None}
        self.gh.comment(6, render_state(state))
        found = {
            "stage": "fix",
            "status": "already_on_base",
            "summary": "",
            "error": "`main` already holds the change of this pull request.",
            "merge_base": "c" * 40,
            "on_base": "b" * 40,
        }
        self.write("nexkit-agent", "result.json", found)
        d = decision("fix", pr=6, target=6, head=HEAD, auto=True)
        outcome = self.run_report(d=d, stored=True)
        self.assertEqual(outcome["outcome"], "already_on_base")
        self.assertEqual(self.gh.pulls[6]["state"], "closed")
        self.assertEqual(self.gh.issues[5]["state_reason"], "completed")
        self.assertFalse(self.gh.merged)
        [row] = self.gh.run_comments(6)
        self.assertEqual(
            (row["run"]["status"], row["run"]["outcome"], row["run"]["on_base"]),
            ("success", "already_on_base", "b" * 40),
        )
        self.assertIn(
            f"already holds the change of this pull request, brought by {'b' * 40}", row["body"]
        )
        self.assertEqual(read_state(self.gh.comments(6))[1]["auto_fixes"], 0)

    def test_an_up_to_date_round_succeeds_without_changes(self):
        self.write("nexkit-agent", "result.json", agent.up_to_date("main"))
        d = decision("fix", pr=6, target=6, head=HEAD, auto=True, conflicts=True)
        outcome = self.run_report(d=d, published=False)
        self.assertEqual(outcome["outcome"], "up_to_date")
        [row] = self.gh.run_comments(6)
        self.assertEqual(row["run"]["status"], "success")
        self.assertEqual(row["run"]["trigger"], "fix (auto, conflicts)")
        self.assertIn("merges cleanly into this pull request now", row["body"])

    def test_a_merge_refused_for_a_conflict_starts_a_conflict_round(self):
        self.candidate()
        self.gh.merge_error = GitHubError(405, "Pull Request is not mergeable")
        self.gh.pulls[6]["mergeable"] = False
        cfg = make_config(merge={"auto": True}, conflicts={"auto_resolve": True})
        self.assertEqual(self.run_report(cfg=cfg)["outcome"], "conflict_fix")
        [dispatch] = rounds_started(self.gh)
        self.assertEqual(dispatch["inputs"]["auto"], "conflicts")
        [row] = self.gh.run_comments(6)
        self.assertIn("It conflicts with `main`. Started an automatic round on #6", row["body"])

    def refused_while_running(self, *runs, max_rounds=3, d=None):
        """A merge refused for a conflict, reported by a round whose own comment still says
        running."""
        os.environ["GITHUB_RUN_ID"] = "1"  # the id the next report runs as
        for run in runs:
            self.gh.comment(6, render_run(run))
        own = {"round": len(runs) + 1, "trigger": "review", "url": run_url(), "status": "running"}
        if d and d.get("conflicts"):
            own.update(trigger="fix (auto, conflicts)", conflicts=True, base_sha=d["base_sha"])
        self.gh.comment(6, render_run(own))
        # A fix round's result fits its schema, so that the round is stored as published.
        self.candidate(output=FIX_OUTPUT if d else None)
        self.gh.merge_error = GitHubError(405, "Pull Request has merge conflicts")
        self.gh.pulls[6]["mergeable"] = False
        conflicts = {"auto_resolve": True, "max_rounds": max_rounds}
        cfg = make_config(merge={"auto": True}, conflicts=conflicts)
        return self.run_report(cfg=cfg, **({"d": d} if d else {}))["outcome"]

    def tried(self, sha):
        return {
            "round": 1,
            "trigger": "fix (auto, conflicts)",
            "url": "https://x/1",
            "status": "failure",
            "conflicts": True,
            "base_sha": sha,
        }

    def test_the_reporting_round_does_not_block_its_own_conflict_round(self):
        self.assertEqual(self.refused_while_running(), "conflict_fix")
        [dispatch] = rounds_started(self.gh)
        self.assertEqual(dispatch["inputs"]["auto"], "conflicts")

    def test_a_refused_merge_tries_each_base_commit_once(self):
        # The conflict round itself reports: the base branch did not move since it started.
        d = decision("fix", pr=6, target=6, head=HEAD, auto=True, conflicts=True)
        d["base_sha"] = BASE_SHA
        self.assertEqual(self.refused_while_running(d=d), "merge_refused")
        self.assertFalse(rounds_started(self.gh))

    def test_a_refused_merge_tries_again_after_another_round_ended(self):
        # A review round ran after the conflict round on the same base commit.
        self.assertEqual(self.refused_while_running(self.tried(BASE_SHA)), "conflict_fix")
        [row] = [r for r in self.gh.run_comments(6) if r["run"]["url"] == run_url()]
        self.assertEqual(row["run"]["conflicts_started"], BASE_SHA)

    def test_a_refused_merge_keeps_the_round_limit(self):
        outcome = self.refused_while_running(self.tried("e" * 40), max_rounds=1)
        self.assertEqual(outcome, "merge_refused")
        self.assertFalse(rounds_started(self.gh))
        self.assertIn("conflict rounds are used up", self.gh.run_comments(6)[-1]["body"])

    def test_a_round_that_stopped_on_an_error_leaves_its_conflict_to_a_person(self):
        # `main` moved while a fix round ran, and the round stopped on an error without trying
        # a merge. Its end sweeps, also without autopilot, but the item failed, and the engine
        # leaves a failed item to a person, so the sweep starts no conflict round.
        self.gh.pulls[6]["mergeable"] = False
        self.write("nexkit-agent", "result.json", {"status": "error", "error": "No change."})
        d = decision("fix", pr=6, target=6, head=HEAD, auto=True)
        cfg = make_config(conflicts={"auto_resolve": True})
        self.assertEqual(self.run_report(d=d, cfg=cfg, published=False)["outcome"], "agent_error")
        [sweep] = self.gh.dispatches
        with mock.patch("nexkit.maintain.time.sleep"):
            maintain(
                self.gh,
                route(self.gh, "workflow_dispatch", {}, sweep["inputs"]),
                cfg,
                workflow="nexkit.yml",
                ref="main",
            )
        self.assertEqual(rounds_started(self.gh), [])

    def test_the_end_of_a_run_sweeps_for_conflicts_unless_a_round_is_on_its_way(self):
        from nexkit import autopilot

        cfg = make_config(conflicts={"auto_resolve": True})

        def sweeps(outcome):
            self.gh.dispatches.clear()
            autopilot.after_run(self.gh, cfg, {"issue": 5}, outcome, "nexkit.yml", "main")
            return len(self.gh.dispatches)

        self.assertEqual([sweeps(o) for o in ("ready", "agent_error", "needs_human")], [1, 1, 1])
        self.assertEqual([sweeps(o) for o in ("auto_fix", "conflict_fix", "paused")], [0, 0, 0])
        # A run whose report did not finish sweeps too, after it ended.
        self.gh.dispatches.clear()
        autopilot.after_unreported(self.gh, cfg, "nexkit.yml", "main", 40)
        self.assertEqual(self.gh.dispatches[0]["inputs"]["note"], "after run 40")
        self.gh.dispatches.clear()
        autopilot.after_run(self.gh, make_config(), {"issue": 5}, "ready", "nexkit.yml", "main")
        self.assertFalse(self.gh.dispatches)

    def test_a_merge_refused_for_another_reason_waits_for_a_person(self):
        self.candidate()
        self.gh.merge_error = GitHubError(405, "Required approving review")
        cfg = make_config(merge={"auto": True}, conflicts={"auto_resolve": True})
        self.assertNotEqual(self.run_report(cfg=cfg)["outcome"], "conflict_fix")
        self.assertFalse(rounds_started(self.gh))

    def test_a_merge_checks_the_other_pull_requests(self):
        self.gh.add_issue(4)
        self.gh.add_pull(7, 4)
        self.gh.pulls[7]["mergeable"] = False
        self.candidate(output=IMPLEMENT_OUTPUT)
        self.run_report(cfg=make_config(merge={"auto": True}))
        self.assertFalse(rounds_started(self.gh))  # off by default
        cfg = make_config(merge={"auto": True}, conflicts={"auto_resolve": True})
        self.reopened(cfg)  # the first merge closed it
        self.run_report(cfg=cfg)
        self.assertEqual([d["inputs"]["number"] for d in rounds_started(self.gh)], ["7"])
        row = self.gh.run_comments(6)[-1]
        self.assertIn("Started an automatic round on #7 to merge `main`.", row["body"])

    def test_a_merge_skips_another_pull_request_whose_round_is_running(self):
        self.gh.add_issue(4)
        self.gh.add_pull(7, 4)
        self.gh.pulls[7]["mergeable"] = False
        self.gh.comment(
            7,
            render_run({"round": 1, "trigger": "fix", "url": "https://x/7/1", "status": "running"}),
        )
        self.candidate()
        cfg = make_config(merge={"auto": True}, conflicts={"auto_resolve": True})
        self.assertEqual(self.run_report(cfg=cfg)["outcome"], "merged")
        self.assertFalse(rounds_started(self.gh))


# A refusal in words NexKit does not read, so only GitHub's data can say why.
UNKNOWN_REFUSAL = GitHubError(405, "Merge refused.")


class MergeRefusalTests(ReportCase):
    """Refusals of the automatic merge that pass by themselves or by NexKit's own rounds."""

    def setUp(self):
        super().setUp()
        sleep = mock.patch("nexkit.report.time.sleep")
        self.sleeps = sleep.start()
        self.addCleanup(sleep.stop)
        # The base branch's rules require `e2e`, a job of the repository's CI that has not
        # reported on the approved head yet.
        self.gh.required = {"e2e"}
        self.cfg = make_config(
            merge={"auto": True},
            conflicts={"auto_resolve": True, "max_rounds": 1},
            autopilot={"enabled": True},
            notify=["olivia"],
        )

    def test_a_merge_refused_because_the_base_branch_moved_is_tried_again(self):
        self.candidate(output=IMPLEMENT_OUTPUT)
        self.gh.required = set()
        modified = GitHubError(405, "Base branch was modified. Review and try the merge again.")
        merge = self.gh.merge

        def refuse(times):
            """`gh.merge` that refuses `times` times, while another merge moves `main`."""
            left = [times]

            def merged(number, sha, method="squash"):
                if not left[0]:
                    return merge(number, sha, method)
                left[0] -= 1
                self.gh.branches["main"] = f"{left[0]}" * 40
                raise modified

            return merged

        with mock.patch.object(self.gh, "merge", side_effect=refuse(2)):
            outcome = self.run_report(cfg=self.cfg)
        self.assertEqual(outcome["outcome"], "merged")
        self.assertIn("at try 3", outcome["summary"])
        self.assertEqual(self.gh.issues[5]["state"], "closed")
        # Three refusals in a row: a person decides.
        self.reopened(self.cfg)
        with mock.patch.object(self.gh, "merge", side_effect=refuse(3)):
            self.assertEqual(self.run_report(cfg=self.cfg)["outcome"], "merge_refused")
        # The same words while the base branch stays where it was: no other try.
        self.reopened(self.cfg)
        self.gh.merge_error = [modified]
        self.assertEqual(self.run_report(cfg=self.cfg)["outcome"], "merge_refused")
        self.assertEqual(len(self.sleeps.call_args_list), 4)

    def test_a_merge_refused_because_the_head_moved_reviews_the_new_head(self):
        self.candidate(output=IMPLEMENT_OUTPUT)
        self.gh.merge_error = GitHubError(
            409, "Head branch was modified. Review and try the merge again."
        )
        # The round checked `b`, which the item's state holds; then the branch moved on.
        self.gh.pulls[6]["head"]["sha"] = "b" * 40
        host.store_import(self.gh, self.cfg, 5)
        self.gh.pulls[6]["head"]["sha"] = "c" * 40
        outcome = self.run_report(cfg=self.cfg, head="b" * 40)
        self.assertEqual(outcome["outcome"], "head_moved")
        self.assertIsNone(outcome.get("attention"))
        self.assertIn(
            "The branch moved to `ccccccc` after this round checked it", outcome["summary"]
        )
        # The review is the next round; no sweep starts, and nobody is mentioned.
        [dispatch] = self.gh.dispatches
        self.assertEqual(
            dispatch["inputs"], {"command": "review", "number": "6", "note": "", "auto": "true"}
        )
        self.assertFalse(self.gh.comments_matching(5, "@olivia"))
        self.assertFalse(self.gh.comments_matching(6, "@olivia"))

    def merge(self, head="c" * 40):
        """`merge_pull` for the pull request that checked `head`: its outcome, text and the
        events the engine heard."""
        from nexkit import host
        from nexkit.report import merge_pull
        from tests.test_report import WORKFLOW

        self.target = {"pr": 6, "head": head, "issue": 5, "base": "main"}
        store_pending(self.gh, self.cfg, 5, 6, head)
        with mock.patch.object(host, "step", wraps=host.step) as step:
            outcome, text = merge_pull(self.gh, self.cfg, self.target, {}, "", WORKFLOW, "main")
        return outcome, text, [call.args[1] for call in step.call_args_list]

    def refusals(self, events):
        return [event["result"]["refusal"] for event in events if event["kind"] == "actionFinished"]

    def test_the_new_head_is_sent_to_the_engine_before_a_moved_head_is_reviewed(self):
        self.gh.merge_error = GitHubError(409, "Head branch was modified.")
        self.gh.pulls[6]["head"]["sha"] = "d" * 40
        outcome, _, events = self.merge(head="b" * 40)
        self.assertEqual(outcome, "head_moved")
        self.assertEqual([event["kind"] for event in events], ["changeObserved", "actionFinished"])
        self.assertEqual(events[0]["version"], "d" * 40)
        self.assertEqual(self.refusals(events), ["headMoved"])
        [dispatch] = self.gh.dispatches
        self.assertEqual(dispatch["inputs"]["command"], "review")

    def test_a_moved_head_that_could_not_be_read_is_a_person_s(self):
        self.cfg = make_config(merge={"auto": True}, conflicts={"auto_resolve": False})
        self.gh.merge_error = GitHubError(409, "Head branch was modified.")
        pull = self.gh.pull
        refused = []

        def read(number):
            if refused:
                raise GitHubError(502, "down")
            return pull(number)

        def merge(number, sha, method="squash"):
            refused.append(number)
            raise self.gh.merge_error

        with mock.patch.object(self.gh, "pull", side_effect=read):
            with mock.patch.object(self.gh, "merge", side_effect=merge):
                outcome, text, events = self.merge(head="b" * 40)
        self.assertEqual(outcome, "merge_refused")
        self.assertEqual(self.refusals(events), ["headMoved"])
        self.assertEqual([event["kind"] for event in events], ["actionFinished"])
        self.assertIn("NexKit could not start a review: GitHub API 502: down", text)
        self.assertEqual(self.gh.dispatches, [])

    def test_a_409_on_the_head_that_was_checked_is_a_person_s(self):
        self.gh.merge_error = GitHubError(409, "Head branch was modified.")
        outcome, text, events = self.merge()
        self.assertEqual(outcome, "merge_refused")
        self.assertEqual(self.refusals(events), ["headMoved"])
        self.assertIn("its head did not move", self.target["found"]["refused"][1])
        self.assertEqual(self.gh.dispatches, [])

    def test_a_review_that_could_not_be_started_is_a_person_s(self):
        from tests.test_report import WORKFLOW

        self.gh.merge_error = GitHubError(409, "Head branch was modified.")
        self.gh.pulls[6]["head"]["sha"] = "d" * 40
        self.gh.dispatch_errors[WORKFLOW] = GitHubError(503, "Service Unavailable")
        outcome, text, _ = self.merge()
        self.assertEqual(outcome, "merge_refused")
        self.assertIn("NexKit could not start a review: GitHub API 503", text)

    def test_the_outcome_of_a_moved_head_follows_the_engine(self):
        from nexkit import host

        self.gh.merge_error = GitHubError(409, "Head branch was modified.")
        self.gh.pulls[6]["head"]["sha"] = "d" * 40
        with mock.patch.object(host, "accept_answer", side_effect=host.Refused("no")):
            outcome, text, _ = self.merge()
        self.assertEqual(outcome, "merge_refused")
        self.assertIn("refused to decide its next step", text)
        self.assertEqual(self.gh.dispatches, [])
        with mock.patch.object(host, "accept_answer", return_value=("person", {})):
            self.assertEqual(self.merge()[0], "merge_refused")
        self.assertEqual(self.gh.dispatches, [])

    def test_the_last_refusal_after_the_base_branch_kept_moving_is_transient(self):
        self.gh.required = set()
        modified = GitHubError(405, "Base branch was modified. Review and try the merge again.")
        left = [3]

        def merge(number, sha, method="squash"):
            left[0] -= 1
            self.gh.branches["main"] = f"{left[0]}" * 40
            raise modified

        with mock.patch.object(self.gh, "merge", side_effect=merge):
            outcome, _, events = self.merge()
        self.assertEqual(outcome, "merge_refused")
        self.assertEqual(self.refusals(events), ["transient"])

    def test_another_refusal_is_one_only_a_person_clears(self):
        self.gh.required = set()
        self.gh.merge_error = UNKNOWN_REFUSAL
        outcome, text, events = self.merge()
        self.assertEqual(outcome, "merge_refused")
        self.assertEqual(self.refusals(events), ["needsPerson"])
        self.assertIn("**What unblocks it:**", text)
        self.assertIn("refused", self.target["found"])

    def test_the_outcome_of_a_refusal_only_a_person_clears_follows_the_engine(self):
        from nexkit import host

        self.gh.required = set()
        self.gh.merge_error = UNKNOWN_REFUSAL
        _, plain, _ = self.merge()
        with mock.patch.object(host, "accept_answer", side_effect=host.Refused("no")):
            outcome, text, _ = self.merge()
        self.assertEqual(outcome, "merge_refused")
        self.assertIn("refused to decide its next step", text)
        self.assertNotIn("refused to decide its next step", plain)

    def test_an_implement_round_whose_review_failed_leaves_its_conflict_to_a_person(self):
        # The engine starts no conflict round for a failed item, though no conflict round was
        # used: the round's comment says a person decides, not that the rounds are used up.
        os.environ["GITHUB_RUN_ID"] = "1"  # the id the next report runs as
        self.gh.comment(5, render_run({"command": "go", "url": run_url(), "status": "running"}))
        self.candidate(review={"status": "error", "error": "Claude stopped (error_max_turns)."})
        self.gh.pulls[6]["mergeable"] = False
        cfg = make_config(conflicts={"auto_resolve": True, "max_rounds": 3})
        outcome = self.run_report(cfg=cfg)
        self.assertEqual(outcome["outcome"], "review_failed")
        self.assertEqual(rounds_started(self.gh), [])
        text = self.gh.run_comments(6)[-1]["body"]
        self.assertIn("It conflicts with `main`.", text)
        self.assertIn("so a person decides", text)
        self.assertNotIn("are used up", text)

    def test_an_implement_round_that_ends_with_a_conflict_starts_its_conflict_round(self):
        os.environ["GITHUB_RUN_ID"] = "1"  # the id the next report runs as
        self.gh.comment(5, render_run({"command": "go", "url": run_url(), "status": "running"}))
        self.candidate(output=IMPLEMENT_OUTPUT)
        self.gh.pulls[6]["mergeable"] = False
        cfg = make_config(conflicts={"auto_resolve": True})
        outcome = self.run_report(cfg=cfg)
        self.assertEqual(outcome["outcome"], "ready")
        [dispatch] = rounds_started(self.gh)
        self.assertEqual(
            (dispatch["inputs"]["number"], dispatch["inputs"]["auto"]), ("6", "conflicts")
        )
        self.assertIn(
            "It conflicts with `main`. Started an automatic round on #6", outcome["summary"]
        )
        # Its round comment, posted as it reports, records the start, so the sweep at its
        # end does not start the round twice.
        self.assertEqual(self.gh.run_comments(6)[-1]["run"]["conflicts_started"], "f" * 40)
        [sweep] = [d for d in self.gh.dispatches if d["inputs"]["command"] == "autopilot"]
        with mock.patch("nexkit.maintain.time.sleep"):
            maintain(
                self.gh,
                route(self.gh, "workflow_dispatch", {}, sweep["inputs"]),
                cfg,
                workflow="nexkit.yml",
                ref="main",
            )
        self.assertEqual(len(rounds_started(self.gh)), 1)
        # Without a conflict, nothing more starts.
        self.gh.dispatches.clear()
        self.gh.pulls[6]["mergeable"] = True
        self.run_report(cfg=cfg)
        self.assertFalse(rounds_started(self.gh))

    def test_an_implement_round_starts_its_conflict_round_only_when_the_engine_answers_one(self):
        os.environ["GITHUB_RUN_ID"] = "1"  # the id the next report runs as
        self.gh.comment(5, render_run({"command": "go", "url": run_url(), "status": "running"}))
        self.candidate(output=IMPLEMENT_OUTPUT)
        self.gh.pulls[6]["mergeable"] = False
        cfg = make_config(conflicts={"auto_resolve": True})
        with mock.patch.object(host, "base_moved", return_value="waits") as moved:
            outcome = self.run_report(cfg=cfg)
        self.assertEqual(outcome["outcome"], "ready")
        self.assertFalse(rounds_started(self.gh))
        self.assertEqual(self.gh.cancelled, [])
        args = moved.call_args.args
        self.assertEqual((args[2], *args[5:]), (5, True, False))
        self.assertNotIn("Started an automatic round", outcome["summary"])

    def test_an_implement_round_whose_conflict_round_the_engine_refuses_starts_none(self):
        os.environ["GITHUB_RUN_ID"] = "1"  # the id the next report runs as
        self.gh.comment(5, render_run({"command": "go", "url": run_url(), "status": "running"}))
        self.candidate(output=IMPLEMENT_OUTPUT)
        self.gh.pulls[6]["mergeable"] = False
        cfg = make_config(conflicts={"auto_resolve": True})
        with mock.patch.object(host, "base_moved", side_effect=host.Refused("no")):
            outcome = self.run_report(cfg=cfg)
        self.assertFalse(rounds_started(self.gh))
        self.assertIn("refused to decide its next step", outcome["summary"])

    def test_a_merge_waiting_for_required_checks_is_merged_by_a_later_sweep(self):
        self.gh.add_milestone(1)
        self.gh.issues[5]["milestone"] = {"number": 1}
        self.candidate(output=IMPLEMENT_OUTPUT)
        pending = GitHubError(405, 'Required status check "e2e" is expected.')
        self.gh.merge_error = [pending]
        outcome = self.run_report(cfg=self.cfg)
        self.assertEqual(outcome["outcome"], "merge_waiting")
        self.assertIsNone(outcome.get("attention"))
        self.assertIn("The next autopilot sweep merges it", outcome["summary"])
        # The checks pass: the next sweep merges and closes the issue.
        self.gh.merge_error = None
        lines = play_sweep(self.gh, self.cfg)
        self.assertIn("#6: merged.", lines)
        self.assertEqual(self.gh.issues[5]["state"], "closed")
        row = self.gh.run_comments(6)[-1]
        self.assertEqual((row["run"]["outcome"], row["run"]["status"]), ("merged", "success"))

    def test_a_merge_waiting_for_checks_without_a_sweep_to_merge_it_waits_for_a_person(self):
        self.gh.add_milestone(1)
        pending = GitHubError(405, 'Required status check "e2e" is expected.')
        # Without autopilot, or outside its scope, a person merges it.
        manual = make_config(merge={"auto": True})
        for cfg in (manual, make_config(merge={"auto": True}, autopilot={"enabled": True})):
            self.gh.merge_error = [pending]
            self.candidate(output=IMPLEMENT_OUTPUT)
            if cfg is not manual:
                self.gh.issues[5]["milestone"] = None
                self.reopened(cfg)  # the second round starts after the first one ended
            outcome = self.run_report(cfg=cfg)
            self.assertEqual(outcome["outcome"], "merge_refused")
            self.assertIn("Merge it once they pass.", outcome["summary"])
            self.assertTrue(outcome["attention"])

    def test_a_failed_merge_step_says_so_and_the_sweep_goes_on(self):
        self.gh.add_milestone(1)
        self.gh.issues[5]["milestone"] = {"number": 1}
        self.candidate(output=IMPLEMENT_OUTPUT)
        self.gh.merge_error = [GitHubError(405, 'Required status check "e2e" is expected.')]
        self.run_report(cfg=self.cfg)
        with mock.patch("nexkit.report.merge_pull", side_effect=GitHubError(502, "Bad")):
            lines = play_sweep(self.gh, self.cfg)
        self.assertIn("Could not merge the pull request of #6: GitHub API 502: Bad", lines)

    def waiting_merge(self):
        """An approved round of #6 whose merge waits for the required check `e2e`."""
        self.gh.add_milestone(1)
        self.gh.issues[5]["milestone"] = {"number": 1}
        self.candidate(output=IMPLEMENT_OUTPUT)
        self.gh.merge_error = [GitHubError(405, 'Required status check "e2e" is in progress.')]
        self.assertEqual(self.run_report(cfg=self.cfg)["outcome"], "merge_waiting")
        self.gh.dispatches.clear()

    def e2e_fails(self):
        """`e2e`, a job of the repository's CI, fails on the approved head in `make e2e`."""
        self.gh.commit_checks[HEAD] = [
            {"id": 77, "name": "e2e", "conclusion": "failure", "html_url": "https://job/77"},
            {"id": 78, "name": "lint", "conclusion": "success", "html_url": "https://job/78"},
        ]
        self.gh.logs[77] = b"##[group]Run make e2e\n##[error]Process completed with exit code 1.\n"
        self.gh.annotations[77] = [
            {"annotation_level": "failure", "title": "Cart > empties", "path": "e2e/cart.spec.ts"}
        ]
        self.gh.merge_error = GitHubError(405, 'Required status check "e2e" is failing.')

    def test_a_merge_waiting_whose_required_check_fails_gets_an_automatic_fix_round(self):
        from nexkit.context import _after_failed_checks
        from nexkit.state import read_state
        from tests.support import feedback as _feedback

        self.waiting_merge()
        self.e2e_fails()
        lines = play_sweep(self.gh, self.cfg)
        self.assertIn("#6: auto_fix.", lines)
        row = self.gh.run_comments(6)[-1]
        self.assertEqual((row["run"]["outcome"], row["run"]["status"]), ("auto_fix", "success"))
        self.assertIn(
            "Required check `e2e` failed on `ccccccc` while the merge waited for it. "
            "Starting automatic fix round 1 of 2.",
            row["body"],
        )
        [fix] = rounds_started(self.gh)
        self.assertEqual(
            fix["inputs"], {"command": "fix", "number": "6", "note": "", "auto": "true"}
        )
        # The round counts toward `fix.max_auto_rounds`, and its feedback names the check.
        state = read_state(self.gh.comments(6))[1]
        self.assertEqual(state["auto_fixes"], 1)
        required = {
            "name": "e2e",
            "url": "https://job/77",
            "commands": ["make e2e"],
            "tests": ["cart.spec.ts::empties"],
        }
        self.assertEqual(state["feedback"]["required"], [required])
        decision = {"pr": 6, "head": HEAD, "auto": True}
        text = _feedback(self.gh, decision, self.gh.comments(6))
        self.assertIn("### Required check `e2e` failed, so GitHub refused the merge", text)
        self.assertIn("Commands that failed in it: `make e2e`.", text)
        # Like any automatic round after failing checks, it merges a base that moved first.
        self.assertTrue(_after_failed_checks(decision, self.gh.comments(6)))
        self.assertFalse(self.gh.comments_matching(6, "@olivia"))
        self.assertFalse(self.gh.comments_matching(5, "@olivia"))

    def test_a_required_check_that_failed_before_the_first_merge_gets_a_fix_round(self):
        # CI failed before NexKit's review ended: the round's own merge is refused.
        from nexkit import progress
        from nexkit.context import _after_failed_checks
        from nexkit.state import read_state

        self.candidate(output=IMPLEMENT_OUTPUT)
        self.e2e_fails()
        self.gh.merge_error = [self.gh.merge_error]
        outcome = self.run_report(cfg=self.cfg)
        self.assertEqual((outcome["outcome"], outcome["attention"]), ("auto_fix", None))
        self.assertIn(
            "Required check `e2e` failed on `ccccccc`, so GitHub refused the merge. "
            "Starting automatic fix round 1 of 2.",
            outcome["summary"],
        )
        # One fix round, counted once, with the failed check in its feedback.
        [fix] = rounds_started(self.gh)
        self.assertEqual(
            fix["inputs"], {"command": "fix", "number": "6", "note": "", "auto": "true"}
        )
        state = read_state(self.gh.comments(6))[1]
        self.assertEqual(state["auto_fixes"], 1)
        self.assertEqual([item["name"] for item in state["feedback"]["required"]], ["e2e"])
        decision = {"pr": 6, "head": HEAD, "auto": True}
        self.assertTrue(_after_failed_checks(decision, self.gh.comments(6)))
        self.assertFalse(self.gh.comments_matching(6, "@olivia"))
        # With no automatic round left, a person decides.
        self.gh.dispatches.clear()
        comment_id, state = read_state(self.gh.comments(6))
        read = json.loads(json.dumps(state))
        state["auto_fixes"] = 2
        progress.save_state(self.gh, 6, comment_id, state, read)
        store_pending(self.gh, self.cfg, 5, 6, HEAD, rounds={"counted": 2})
        self.gh.merge_error = [GitHubError(405, 'Required status check "e2e" is failing.')]
        self.assertEqual(self.run_report(cfg=self.cfg)["outcome"], "needs_human")
        self.assertFalse(rounds_started(self.gh))

    def test_a_merge_waiting_whose_required_check_fails_without_rounds_left_waits(self):
        from nexkit import progress
        from nexkit.state import read_state

        self.waiting_merge()
        comment_id, state = read_state(self.gh.comments(6))
        read = json.loads(json.dumps(state))
        state["auto_fixes"] = 2
        progress.save_state(self.gh, 6, comment_id, state, read)
        store_fields(self.gh, 5, rounds={"counted": 2})
        self.e2e_fails()
        play_sweep(self.gh, self.cfg)
        row = self.gh.run_comments(6)[-1]["run"]
        self.assertEqual(row["outcome"], "needs_human")
        self.assertIn("no automatic fix rounds remain", row["attention"])
        self.assertFalse(rounds_started(self.gh))
        self.assertEqual(read_state(self.gh.comments(6))[1]["auto_fixes"], 2)
        self.assertTrue(self.gh.comments_matching(6, "@olivia a person is needed"))

    def test_a_merge_waiting_whose_checks_never_pass_waits_for_a_person_after_hours(self):
        from datetime import timedelta

        from nexkit import autopilot

        self.waiting_merge()
        later = datetime.now(UTC) + timedelta(hours=7)
        [step] = [s for s in autopilot.preview(self.gh, self.cfg)[1] if s["number"] == 5]
        self.assertEqual(step["state"], "merge")
        step = autopilot.assess(self.gh, self.cfg, self.gh.issues[5], {5: 6}, now=later)
        self.assertEqual((step["state"], step["person"]), ("waits", True))
        self.assertIn("have not passed for six hours", step["reason"])

    def test_the_six_hours_of_a_merge_waiting_for_checks_end_as_the_engine_answers(self):
        from datetime import timedelta

        from nexkit import autopilot, host

        self.waiting_merge()
        # The sweep decides on the stored state, which holds the start of the wait.
        self.assertTrue(item_state.read(self.gh, 5)[1]["checksSince"])
        later = datetime.now(UTC) + timedelta(hours=7)
        real = host.step

        def early(state, event, policies):
            """The engine's answer to the tick as if it came when the wait began."""
            return real(state, {**event, "at": state["checksSince"]["at"]}, policies)

        with mock.patch.object(host, "step", side_effect=early) as step:
            step_ = autopilot.assess(self.gh, self.cfg, self.gh.issues[5], {5: 6}, now=later)
        self.assertEqual(step_["state"], "merge")
        [(state, event, _)] = [call.args for call in step.call_args_list]
        self.assertEqual((event["kind"], event["at"]), ("tick", iso(later)))
        self.assertEqual(state["waits"][0]["id"], event["wait"])
        # An answer the host cannot carry out leaves that issue to a person, alone.
        with mock.patch.object(host, "step", side_effect=host.Refused("no")):
            step_ = autopilot.assess(self.gh, self.cfg, self.gh.issues[5], {5: 6}, now=later)
        self.assertEqual((step_["state"], step_["person"]), ("waits", True))
        self.assertIn("refused to decide", step_["reason"])
        # A branch behind its base is sent as observed so, and the engine keeps its merge going
        # when `conflicts.auto_resolve` updates it, past the six hours.
        self.gh.pulls[6]["mergeable_state"] = "behind"
        with mock.patch.object(host, "step", wraps=host.step) as step:
            step_ = autopilot.assess(self.gh, self.cfg, self.gh.issues[5], {5: 6}, now=later)
        self.assertEqual(step_["state"], "merge")
        [(_, observed, _), (state, tick, _)] = [call.args for call in step.call_args_list]
        self.assertEqual((observed["kind"], observed["behind"]), ("changeObserved", True))
        self.assertEqual((tick["kind"], state["observed"]["behind"]), ("tick", True))
        # Without it, a person looks into the checks, as for any other branch.
        with mock.patch.dict(self.cfg["conflicts"], {"auto_resolve": False}):
            step_ = autopilot.assess(self.gh, self.cfg, self.gh.issues[5], {5: 6}, now=later)
        self.assertEqual((step_["state"], step_["person"]), ("waits", True))
        self.assertIn("have not passed for six hours", step_["reason"])

    def test_a_new_commit_on_a_merge_waiting_pull_request_is_reviewed(self):
        self.waiting_merge()
        self.gh.pulls[6]["head"]["sha"] = "e" * 40
        lines = play_sweep(self.gh, self.cfg)
        self.assertIn(
            "Started /nexkit review on #6: pull request #6 got the new commit `eeeeeee` while "
            "its merge waited for required checks.",
            lines,
        )
        [review] = rounds_started(self.gh)
        self.assertEqual(
            review["inputs"], {"command": "review", "number": "6", "note": "", "auto": "true"}
        )
        self.assertFalse(self.gh.merged)
        # It starts once: the next sweep waits for it, and nobody is mentioned.
        play_sweep(self.gh, self.cfg)
        self.assertEqual(len(rounds_started(self.gh)), 1)
        self.assertFalse(self.gh.comments_matching(5, "@olivia"))
        self.assertFalse(self.gh.comments_matching(6, "@olivia"))

    def test_a_branch_behind_its_base_is_updated_and_reviewed_again(self):
        self.candidate(output=IMPLEMENT_OUTPUT)
        self.gh.pulls[6]["mergeable_state"] = "behind"
        self.gh.merge_error = GitHubError(405, "Head branch is out of date. Review and try again.")
        outcome = self.run_report(cfg=self.cfg)
        self.assertEqual(outcome["outcome"], "branch_updated")
        self.assertEqual(self.gh.updated, [(6, HEAD)])
        [dispatch] = self.gh.dispatches
        self.assertEqual(dispatch["inputs"]["command"], "review")
        self.assertTrue(self.gh.run_comments(6)[-1]["run"]["updated_branch"])
        # Once per `conflicts.max_rounds`: a person decides after that.
        # The review that follows the update stores it as used (`command`), and a later merge
        # of the same head is refused again.
        self.gh.pulls[6]["head"]["sha"] = HEAD
        self.gh.pulls[6]["mergeable_state"] = "behind"
        self.reopened(self.cfg)  # the review round that follows the update
        store_fields(self.gh, 5, rounds={"updates": 1})
        self.assertEqual(self.run_report(cfg=self.cfg)["outcome"], "merge_refused")
        self.assertEqual(len(self.gh.updated), 1)

    def test_required_checks_expected_on_a_branch_behind_its_base_update_it(self):
        # Branch protection wants the branch up to date: GitHub reports the checks that
        # passed on its head as "expected", and the pull request as behind.
        self.candidate(output=IMPLEMENT_OUTPUT)
        self.gh.pulls[6]["mergeable_state"] = "behind"
        expected = "Repository rule violations found\n\n2 of 2 required status checks are expected."
        self.gh.merge_error = [GitHubError(405, expected)]
        outcome = self.run_report(cfg=self.cfg)
        self.assertEqual(outcome["outcome"], "branch_updated")
        self.assertIn("NexKit merged `main` into it", outcome["summary"])
        self.assertEqual(self.gh.updated, [(6, HEAD)])
        [review] = rounds_started(self.gh)
        self.assertEqual(
            review["inputs"], {"command": "review", "number": "6", "note": "", "auto": "true,carry"}
        )
        self.assertTrue(self.gh.run_comments(6)[-1]["run"]["updated_branch"])

    def test_required_checks_expected_on_a_branch_not_behind_wait(self):
        self.gh.add_milestone(1)
        self.gh.issues[5]["milestone"] = {"number": 1}
        self.candidate(output=IMPLEMENT_OUTPUT)
        expected = "Repository rule violations found\n\n2 of 2 required status checks are expected."
        for state in (None, "blocked"):
            self.reopened(self.cfg)  # each report is the end of a round that started
            if state:
                self.gh.pulls[6]["mergeable_state"] = state
            self.gh.merge_error = [GitHubError(405, expected)]
            self.assertEqual(self.run_report(cfg=self.cfg)["outcome"], "merge_waiting")
            self.assertFalse(self.gh.updated)
        # Without `conflicts.auto_resolve`, a branch that is behind waits too.
        cfg = make_config(merge={"auto": True}, autopilot={"enabled": True})
        self.reopened(cfg)
        self.gh.pulls[6]["mergeable_state"] = "behind"
        self.gh.merge_error = [GitHubError(405, expected)]
        self.assertEqual(self.run_report(cfg=cfg)["outcome"], "merge_waiting")
        self.assertFalse(self.gh.updated)

    def test_a_merge_waiting_on_a_branch_behind_its_base_is_updated_by_the_sweep(self):
        from datetime import timedelta

        from nexkit import autopilot

        # Hours in `merge_waiting`, behind `main`, as before NexKit counted it as behind.
        self.waiting_merge()
        self.gh.pulls[6]["mergeable_state"] = "behind"
        later = datetime.now(UTC) + timedelta(hours=7)
        step = autopilot.assess(self.gh, self.cfg, self.gh.issues[5], {5: 6}, now=later)
        self.assertEqual(step["state"], "merge")
        expected = "2 of 2 required status checks are expected."
        self.gh.merge_error = [GitHubError(405, expected)]
        lines = play_sweep(self.gh, self.cfg)
        self.assertIn("#6: branch_updated.", lines)
        self.assertEqual(self.gh.updated, [(6, HEAD)])
        [review] = rounds_started(self.gh)
        self.assertEqual(review["inputs"]["auto"], "true,carry")
        row = self.gh.run_comments(6)[-1]["run"]
        self.assertEqual((row["outcome"], row.get("attention")), ("branch_updated", None))
        self.assertFalse(self.gh.comments_matching(6, "@olivia"))

    def test_the_review_after_an_update_finds_the_approval_it_carries_over(self):
        # An implement round's report runs beside the review it starts, which reads the
        # approval from the state comment: it is saved before the review is started.
        self.candidate(output=IMPLEMENT_OUTPUT)
        self.gh.pulls[6]["mergeable_state"] = "behind"
        self.gh.merge_error = GitHubError(405, "Head branch is out of date. Review and try again.")
        seen, dispatch = [], self.gh.dispatch

        def dispatched(workflow, ref, inputs, run_details=False):
            if inputs.get("command") == "review":
                seen.append((inputs["auto"], approved_head(self.gh.comments(6))))
            return dispatch(workflow, ref, inputs, run_details)

        self.gh.dispatch = dispatched
        self.assertEqual(self.run_report(cfg=self.cfg)["outcome"], "branch_updated")
        self.assertEqual(seen, [("true,carry", HEAD)])
        # One state comment, edited in place afterwards.
        self.assertEqual(len(self.gh.comments_matching(6, "nexkit:state")), 1)

    def test_a_required_check_that_failed_is_found_from_githubs_data(self):
        # Words NexKit does not know: the base branch's rules and the head's checks decide.
        self.candidate(output=IMPLEMENT_OUTPUT)
        self.e2e_fails()
        self.gh.required = {"e2e", "lint"}
        self.gh.merge_error = [UNKNOWN_REFUSAL]
        outcome = self.run_report(cfg=self.cfg)
        self.assertEqual(outcome["outcome"], "auto_fix")
        self.assertIn("Required check `e2e` failed on `ccccccc`", outcome["summary"])
        state = read_state(self.gh.comments(6))[1]
        self.assertEqual([item["name"] for item in state["feedback"]["required"]], ["e2e"])
        self.assertEqual(state["feedback"]["required"][0]["commands"], ["make e2e"])

    def test_required_checks_that_have_not_reported_are_found_from_githubs_data(self):
        self.gh.add_milestone(1)
        self.gh.issues[5]["milestone"] = {"number": 1}
        self.candidate(output=IMPLEMENT_OUTPUT)
        # A check run still in progress, a commit status still pending, a check not seen yet.
        for required, checks, statuses in (
            ({"e2e"}, [{"name": "e2e", "status": "in_progress", "conclusion": None}], []),
            ({"ci/build"}, [], [("ci/build", "pending")]),
            ({"deploy-preview"}, [], []),
        ):
            with self.subTest(required=required):
                self.reopened(self.cfg)  # each report is the end of a round that started
                self.gh.required = required
                self.gh.commit_checks[HEAD] = checks
                for context, state in statuses:
                    self.gh.set_status(HEAD, context, state, "")
                self.gh.merge_error = [UNKNOWN_REFUSAL]
                self.assertEqual(self.run_report(cfg=self.cfg)["outcome"], "merge_waiting")
        # A failed commit status is a failed check, as a check run is.
        self.reopened(self.cfg)
        self.gh.required = {"ci/build"}
        self.gh.set_status(HEAD, "ci/build", "failure", "")
        self.gh.merge_error = [UNKNOWN_REFUSAL]
        self.assertEqual(self.run_report(cfg=self.cfg)["outcome"], "auto_fix")

    def test_a_branch_behind_or_a_moved_head_is_found_from_githubs_data(self):
        self.candidate(output=IMPLEMENT_OUTPUT)
        self.gh.pulls[6]["mergeable_state"] = "behind"
        self.gh.merge_error = [UNKNOWN_REFUSAL]
        self.assertEqual(self.run_report(cfg=self.cfg)["outcome"], "branch_updated")
        # GitHub answers 409 when the head is not the commit it was asked to merge, and the
        # pull request names its new head.
        for moved, error in (("9", GitHubError(409, "Conflict")), ("8", UNKNOWN_REFUSAL)):
            with self.subTest(error=error):
                # Each refusal ends a round that checked `HEAD`, before the branch moved.
                self.gh.pulls[6]["head"]["sha"] = HEAD
                self.reopened(self.cfg)
                self.gh.pulls[6]["head"]["sha"] = moved * 40
                self.gh.pulls[6].pop("mergeable_state", None)
                self.gh.merge_error = [error]
                self.assertEqual(self.run_report(cfg=self.cfg)["outcome"], "head_moved")

    def test_required_checks_that_github_marks_on_the_pull_request_count(self):
        # Classic protection the token may not read: GitHub still marks the checks that
        # reported on the head as required for this pull request.
        self.gh.add_milestone(1)
        self.gh.issues[5]["milestone"] = {"number": 1}
        self.candidate(output=IMPLEMENT_OUTPUT)
        self.gh.required, self.gh.required_complete = set(), False
        for state, outcome in (("pending", "merge_waiting"), ("failed", "auto_fix")):
            with self.subTest(state=state):
                self.reopened(self.cfg)  # each report is the end of a round that started
                self.gh.contexts[6] = [{"name": "Unit tests", "sha": HEAD, "state": state}]
                self.gh.merge_error = [UNKNOWN_REFUSAL]
                self.assertEqual(self.run_report(cfg=self.cfg)["outcome"], outcome)
        state = read_state(self.gh.comments(6))[1]
        self.assertEqual([item["name"] for item in state["feedback"]["required"]], ["Unit tests"])
        # A check of another commit, or one that passed, says nothing about this head.
        for found in ({"sha": "9" * 40, "state": "failed"}, {"sha": HEAD, "state": "passed"}):
            self.reopened(self.cfg)
            self.gh.contexts[6] = [{"name": "Unit tests", **found}]
            self.gh.merge_error = [UNKNOWN_REFUSAL]
            self.assertEqual(self.run_report(cfg=self.cfg)["outcome"], "merge_refused")

    def test_githubs_words_count_only_for_checks_its_data_cannot_name(self):
        self.gh.add_milestone(1)
        self.gh.issues[5]["milestone"] = {"number": 1}
        self.candidate(output=IMPLEMENT_OUTPUT)
        self.gh.required = set()
        expected = GitHubError(405, 'Required status check "deploy-preview" is expected.')
        failing = GitHubError(405, 'Required status check "deploy-preview" is failing.')
        # The words count when the token may not read every required check, and GitHub
        # holds the pull request back; never for a failed check, which GitHub marks.
        for complete, state, error, outcome in (
            (False, "blocked", expected, "merge_waiting"),
            (False, "behind", expected, "merge_waiting"),
            (True, "blocked", expected, "merge_refused"),
            (False, "clean", expected, "merge_refused"),
            (False, "blocked", failing, "merge_refused"),
        ):
            with self.subTest(complete=complete, state=state, error=str(error)):
                cfg = make_config(merge={"auto": True}, autopilot={"enabled": True})
                self.reopened(cfg)  # each report is the end of a round that started
                self.gh.required_complete = complete
                self.gh.pulls[6]["mergeable_state"] = state
                self.gh.merge_error = [error]
                self.assertEqual(self.run_report(cfg=cfg)["outcome"], outcome)

    def test_a_mergeable_state_github_has_not_worked_out_is_asked_again(self):
        from nexkit.report import merge_state

        self.candidate(output=IMPLEMENT_OUTPUT)
        answers = iter(["unknown", "unknown", "behind"])
        with mock.patch.object(
            self.gh, "pull", side_effect=lambda n: {"mergeable_state": next(answers)}
        ):
            self.assertEqual(merge_state(self.gh, 6), "behind")
        self.assertEqual(len(self.sleeps.call_args_list), 2)
        with mock.patch.object(self.gh, "pull", return_value={"mergeable_state": "unknown"}):
            self.assertIsNone(merge_state(self.gh, 6))
        with mock.patch.object(self.gh, "pull", side_effect=GitHubError(502, "Bad")):
            self.assertIsNone(merge_state(self.gh, 6))

    def test_a_refusal_that_needs_a_person_names_githubs_reason(self):
        self.candidate(output=IMPLEMENT_OUTPUT)
        self.gh.required = set()
        self.gh.merge_error = GitHubError(405, "At least 1 approving review is required.")
        outcome = self.run_report(cfg=self.cfg)
        self.assertEqual(outcome["outcome"], "merge_refused")
        self.assertIn("At least 1 approving review is required", outcome["summary"])
        self.assertFalse(self.gh.dispatches[:-1])  # only the autopilot sweep after the run
        self.assertEqual(self.gh.updated, [])


class MergeConflictEngineTests(ReportCase):
    """A merge refused for a conflict, or because the branch is behind its base, is the
    engine's to decide (`host.accept_answer`): the host sends the refusal with the conflict
    rounds or the updates used, and carries out the answer."""

    def setUp(self):
        super().setUp()
        for target in ("nexkit.report.time.sleep", "nexkit.maintain.time.sleep"):
            patch = mock.patch(target)
            patch.start()
            self.addCleanup(patch.stop)
        self.gh.required = set()
        self.gh.merge_error = GitHubError(405, "Pull Request is not mergeable")
        self.candidate(output=IMPLEMENT_OUTPUT)

    def config(self, auto_resolve=True, max_rounds=3):
        conflicts = {"auto_resolve": auto_resolve, "max_rounds": max_rounds}
        return make_config(merge={"auto": True}, conflicts=conflicts)

    def sent(self, cfg, **mutate):
        """The accept results that the engine received for the merge, and the report's
        outcome, after the pull request is as `mutate` says."""
        self.gh.pulls[6].update(mutate)
        # The report's own round started after the rounds that came before it, and the item's
        # state holds it.
        progress.start(self.gh, decision())
        host.store_import(self.gh, cfg, 5)
        with mock.patch.object(host, "step", wraps=host.step) as step:
            outcome = self.run_report(cfg=cfg)
        sent = [
            (call.args[0], call.args[1])
            for call in step.call_args_list
            if call.args[1]["kind"] == "actionFinished" and "refusal" in call.args[1]["result"]
        ]
        return sent, outcome

    def test_a_conflict_goes_to_the_engine_with_the_rounds_used(self):
        [(doc, event)], outcome = self.sent(self.config(), mergeable=False)
        self.assertEqual(event["result"]["refusal"], "conflict")
        self.assertEqual(doc["rounds"]["conflicts"], 0)
        self.assertEqual(outcome["outcome"], "conflict_fix")
        self.assertEqual(len(rounds_started(self.gh)), 1)

    def test_a_conflict_with_the_rounds_used_up_waits_for_a_person(self):
        self.gh.comment(6, render_run(self.tried_round()))
        [(doc, event)], outcome = self.sent(self.config(max_rounds=1), mergeable=False)
        self.assertEqual(event["result"]["refusal"], "conflict")
        self.assertEqual(doc["rounds"]["conflicts"], 1)
        self.assertEqual(outcome["outcome"], "merge_refused")
        self.assertIn("conflict rounds are used up", outcome["summary"])
        self.assertFalse(rounds_started(self.gh))

    def tried_round(self):
        return {
            "round": 1,
            "trigger": "fix (auto, conflicts)",
            "url": "https://x/1",
            "status": "failure",
            "conflicts": True,
            "base_sha": "e" * 40,
        }

    def test_a_dirty_pull_request_is_a_conflict_without_auto_resolve(self):
        sent, outcome = self.sent(self.config(False), mergeable_state="dirty")
        [(doc, event)] = sent
        self.assertEqual(event["result"]["refusal"], "conflict")
        self.assertEqual(outcome["outcome"], "merge_refused")
        do, why = _not_up_to_date("dirty", "main")
        self.assertIn(f"**What unblocks it:** {autopilot.person(do, why)}.", outcome["summary"])
        self.assertFalse(rounds_started(self.gh))

    def test_a_dirty_pull_request_found_only_at_the_end_needs_a_person(self):
        sent, outcome = self.sent(self.config(), mergeable=None, mergeable_state="dirty")
        [(doc, event)] = sent
        self.assertEqual(event["result"]["refusal"], "needsPerson")
        self.assertEqual(outcome["outcome"], "merge_refused")
        do, why = _not_up_to_date("dirty", "main")
        self.assertIn(f"**What unblocks it:** {autopilot.person(do, why)}.", outcome["summary"])
        self.assertFalse(rounds_started(self.gh))

    def updated_round(self, n):
        return render_run(
            {
                "round": 10 + n,
                "trigger": "review",
                "updated_branch": True,
                "status": "success",
                "url": f"https://x/runs/{n}",
            }
        )

    def sent_all(self, cfg, **mutate):
        """Every result that the engine received for the merge, in order, and the outcome."""
        self.gh.pulls[6].update(mutate)
        # The report's own round started after the rounds that came before it, and the item's
        # state holds it.
        progress.start(self.gh, decision())
        host.store_import(self.gh, cfg, 5)
        with mock.patch.object(host, "step", wraps=host.step) as step:
            outcome = self.run_report(cfg=cfg)
        sent = [
            (call.args[0], call.args[1])
            for call in step.call_args_list
            if call.args[1]["kind"] == "actionFinished"
            # The refusals, not the publish result that says it refused nothing.
            and ("refusal" in call.args[1]["result"] or call.args[1]["result"].get("refused"))
        ]
        return sent, outcome

    def test_a_branch_behind_its_base_goes_to_the_engine_and_is_updated(self):
        [(doc, event)], outcome = self.sent_all(self.config(), mergeable_state="behind")
        self.assertEqual(event["result"]["refusal"], "behind")
        self.assertNotIn("updates", doc["rounds"])
        self.assertEqual(doc["rounds"]["conflicts"], 0)
        self.assertEqual(outcome["outcome"], "branch_updated")
        self.assertIn("It is behind `main`", outcome["summary"])
        self.assertEqual(self.gh.updated, [(6, HEAD)])
        self.assertTrue(self.gh.run_comments(6)[-1]["run"]["updated_branch"])

    def test_the_updates_used_go_to_the_engine_apart_from_the_conflict_rounds(self):
        self.gh.comment(6, self.updated_round(1))
        [(doc, event)], outcome = self.sent_all(self.config(max_rounds=2), mergeable_state="behind")
        self.assertEqual((doc["rounds"]["updates"], doc["rounds"]["conflicts"]), (1, 0))
        self.assertEqual(outcome["outcome"], "branch_updated")
        self.assertEqual(len(self.gh.updated), 1)

    def test_a_branch_behind_with_its_updates_used_up_waits_for_a_person(self):
        self.gh.comment(6, self.updated_round(1))
        [(doc, event)], outcome = self.sent_all(self.config(max_rounds=1), mergeable_state="behind")
        self.assertEqual((event["result"]["refusal"], doc["rounds"]["updates"]), ("behind", 1))
        self.assertEqual(outcome["outcome"], "merge_refused")
        used = "Its 1 automatic updates of the branch from `main` are used up."
        self.assertIn(used, outcome["summary"])
        self.assertEqual((self.gh.updated, rounds_started(self.gh)), ([], []))

    def test_an_update_that_github_refuses_goes_back_to_the_engine_and_waits_for_a_person(self):
        error = GitHubError(422, "Merge conflict")
        with mock.patch.object(self.gh, "update_branch", side_effect=error):
            [(_, first), (doc, second)], outcome = self.sent_all(
                self.config(), mergeable_state="behind"
            )
        self.assertEqual(first["result"]["refusal"], "behind")
        self.assertEqual(second["result"]["change"], None)
        self.assertIn("could not update the branch", second["result"]["refused"])
        self.assertEqual([action["kind"] for action in doc["pending"]], ["updateChange"])
        self.assertEqual(outcome["outcome"], "merge_refused")
        self.assertIn("NexKit could not update the branch: ", outcome["summary"])
        self.assertFalse(rounds_started(self.gh))

    def test_a_branch_behind_without_auto_resolve_is_a_person_s(self):
        [(doc, event)], outcome = self.sent_all(self.config(False), mergeable_state="behind")
        self.assertEqual(event["result"]["refusal"], "behind")
        self.assertEqual(outcome["outcome"], "merge_refused")
        do, why = _not_up_to_date("behind", "main")
        self.assertIn(f"**What unblocks it:** {autopilot.person(do, why)}.", outcome["summary"])
        self.assertEqual((self.gh.updated, rounds_started(self.gh)), ([], []))

    def test_an_answer_the_host_cannot_carry_out_waits_for_a_person_and_says_why(self):
        for kwargs in (
            {"mergeable": False},
            {"mergeable": None, "mergeable_state": "dirty"},
            {"mergeable_state": "behind"},
        ):
            with self.subTest(**kwargs):
                self.reopened(self.config())  # each report is the end of a round that started
                self.gh.pulls[6].update(kwargs)
                with mock.patch.object(host, "accept_answer", side_effect=host.Refused("No.")):
                    outcome = self.run_report(cfg=self.config())
                self.assertEqual(outcome["outcome"], "merge_refused")
                said = "NexKit's engine refused to decide its next step: `No.`"
                self.assertIn(said, outcome["summary"])
                self.assertFalse(rounds_started(self.gh))


if __name__ == "__main__":
    unittest.main()
