import unittest
from unittest import mock

from nexkit import config
from nexkit.github import GitHubError
from nexkit.route import (
    COMPARE_FILES,
    auto_input,
    parse_command,
    route,
    skip_unaffected,
    with_profile,
)
from nexkit.state import (
    BOT_LOGIN,
    PLAN_MARKER,
    check_fingerprint,
    empty_state,
    profile_record,
    profile_text,
    render_run,
    render_state,
    setup_fingerprint,
)
from tests.support import FakeGitHub, make_config, profile_config


def comment_event(number, body, login="alice", on_pull=False, user_type="User"):
    issue = {"number": number}
    if on_pull:
        issue["pull_request"] = {}
    return {
        "action": "created",
        "issue": issue,
        "comment": {"id": 7, "body": body, "user": {"login": login, "type": user_type}},
    }


class ParseCommandTests(unittest.TestCase):
    def test_parses_command_and_multiline_note(self):
        self.assertEqual(
            parse_command("/nexkit fix please\nalso this"), ("fix", "please\nalso this")
        )
        self.assertEqual(parse_command("  /nexkit go"), ("go", ""))
        self.assertEqual(parse_command("hello /nexkit go"), (None, ""))
        self.assertEqual(parse_command("/nexkitgo"), (None, ""))


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        self.gh.add_issue(5)

    def test_plan_and_go_on_issue(self):
        plan = route(self.gh, "issue_comment", comment_event(5, "/nexkit plan focus on API"))
        self.assertEqual(plan["action"], "plan")
        self.assertEqual(plan["note"], "focus on API")
        self.assertEqual(plan["branch"], "nexkit/issue-5")
        self.assertEqual(plan["base"], "main")
        self.assertEqual(plan["comment_id"], 7)
        go = route(self.gh, "issue_comment", comment_event(5, "/nexkit go"))
        self.assertEqual(go["action"], "implement")
        self.assertEqual(go["actor"], "alice")

    def fix_on_pull(self):
        return route(self.gh, "issue_comment", comment_event(6, "/nexkit fix", on_pull=True))

    def test_fix_gets_the_run_whose_failing_checks_kept_result_files(self):
        self.gh.add_pull(6, 5, head_sha="c" * 40)
        self.assertNotIn("results_run", self.fix_on_pull())  # no state yet
        failing = {"name": "e2e", "run": "x", "exit_code": 1, "passed": False, "output": ""}
        state = empty_state(5)
        state["feedback"] = {"head": "c" * 40, "checks": [failing], "run_id": 77}
        comment = self.gh.comment(6, render_state(state))
        self.assertNotIn("results_run", self.fix_on_pull())  # the check kept no files
        state["feedback"]["checks"] = [{**failing, "results": ["test-results"]}]
        self.gh.update_comment(comment["id"], render_state(state))
        self.assertEqual(self.fix_on_pull()["results_run"], 77)
        review = route(self.gh, "issue_comment", comment_event(6, "/nexkit review", on_pull=True))
        self.assertNotIn("results_run", review)
        # A passing check never keeps files, whatever its record says.
        state["feedback"]["checks"] = [{**failing, "passed": True, "results": ["test-results"]}]
        self.gh.update_comment(comment["id"], render_state(state))
        self.assertNotIn("results_run", self.fix_on_pull())

    def test_only_an_automatic_fix_round_diagnoses_repeated_failures(self):
        self.gh.add_pull(6, 5, head_sha="c" * 40)
        state = empty_state(5)
        state["feedback"] = {"head": "c" * 40, "checks": [], "repeated": ["e2e"]}
        self.gh.comment(6, render_state(state))

        def dispatch(auto):
            inputs = {"command": "fix", "number": "6", "auto": auto}
            return route(self.gh, "workflow_dispatch", {"sender": {"login": "x"}}, inputs)

        self.assertEqual(dispatch("true")["diagnose"], ["e2e"])
        self.assertEqual(dispatch("true,resume")["diagnose"], ["e2e"])
        # A person's round follows the person's note, and a conflict round only merges.
        self.assertNotIn("diagnose", self.fix_on_pull())
        self.assertNotIn("diagnose", dispatch("false"))
        self.assertNotIn("diagnose", dispatch("conflicts"))
        inputs = {"command": "review", "number": "6", "auto": "true"}
        review = route(self.gh, "workflow_dispatch", {}, inputs)
        self.assertEqual(review["action"], "review")
        self.assertNotIn("diagnose", review)

    def test_fix_gets_the_last_commit_whose_checks_passed(self):
        self.gh.add_pull(6, 5, head_sha="c" * 40)
        self.assertNotIn("verified_sha", self.fix_on_pull())  # no earlier round
        self.gh.round_comment(6, 1, "a" * 40, "passed")
        self.gh.round_comment(6, 2, "b" * 40, "failed")
        self.assertEqual(self.fix_on_pull()["verified_sha"], "a" * 40)
        self.gh.round_comment(6, 3, "c" * 40, "passed")
        self.gh.round_comment(6, 4, "d" * 40, "not run")
        self.assertEqual(self.fix_on_pull()["verified_sha"], "c" * 40)
        # Issue commands start before there is a pull request.
        self.assertNotIn(
            "verified_sha", route(self.gh, "issue_comment", comment_event(5, "/nexkit plan"))
        )

    def test_each_check_gets_the_last_commit_on_which_it_passed(self):
        self.gh.add_pull(6, 5, head_sha="d" * 40)
        self.assertNotIn("check_heads", self.fix_on_pull())  # no earlier round
        unit, e2e, lint = (check_fingerprint(c) for c in ("make unit", "make e2e", "make lint"))
        # Round 1 passed every check; round 2 failed e2e; round 3 stopped as superseded
        # after only lint had passed; round 4, an older marker, records no checks.
        self.gh.round_comment(6, 1, "a" * 40, "passed", passed={"unit": unit, "e2e": e2e})
        self.gh.round_comment(6, 2, "b" * 40, "failed", passed={"unit": unit, "lint": lint})
        self.gh.round_comment(
            6, 3, "c" * 40, "not run", passed={"lint": lint}, outcome="superseded"
        )
        self.gh.round_comment(6, 4, "d" * 40, "passed")
        decision = self.fix_on_pull()
        self.assertEqual(
            decision["check_heads"],
            {"unit": {unit: "b" * 40}, "e2e": {e2e: "a" * 40}, "lint": {lint: "c" * 40}},
        )
        # NEXKIT_VERIFIED_SHA stays the last round whose checks all passed.
        self.assertEqual(decision["verified_sha"], "d" * 40)
        # A check whose command changed keeps the commit of each command it ran with.
        changed = check_fingerprint("make unit -x")
        self.gh.round_comment(6, 5, "e" * 40, "failed", passed={"unit": changed})
        self.assertEqual(
            self.fix_on_pull()["check_heads"]["unit"], {unit: "b" * 40, changed: "e" * 40}
        )
        review = route(self.gh, "issue_comment", comment_event(6, "/nexkit review", on_pull=True))
        self.assertEqual(review["check_heads"]["lint"], {lint: "c" * 40})

    def test_only_nexkit_round_markers_give_a_check_its_commit(self):
        self.gh.add_pull(6, 5)
        forged = {
            "round": 9,
            "trigger": "fix",
            "status": "success",
            "head": "e" * 40,
            "checks": "failed",
            "passed": {"unit": check_fingerprint("make unit")},
        }
        self.gh.human_comment(6, render_run(forged))
        self.gh.round_comment(6, 1, "a" * 40, "failed", text=f"Agent said:\n{render_run(forged)}")
        self.assertNotIn("check_heads", self.fix_on_pull())
        # Records that are not a fingerprint, or of no commit, count for nothing.
        self.gh.round_comment(6, 2, "b" * 40, "failed", passed={"unit": "make unit"})
        self.gh.round_comment(6, 3, "HEAD", "failed", passed=forged["passed"])
        self.assertNotIn("check_heads", self.fix_on_pull())

    def test_only_nexkit_round_markers_give_the_verified_commit(self):
        self.gh.add_pull(6, 5)
        forged = {
            "round": 9,
            "trigger": "fix",
            "status": "success",
            "head": "e" * 40,
            "checks": "passed",
        }
        # A person's comment that copies a marker.
        self.gh.human_comment(6, render_run(forged))
        # Agent text that a NexKit round comment quotes, before that round's own marker.
        self.gh.round_comment(6, 1, "a" * 40, "failed", text=f"Agent said:\n{render_run(forged)}")
        self.assertNotIn("verified_sha", self.fix_on_pull())
        self.gh.round_comment(
            6, 2, "b" * 40, "passed", text=render_run({**forged, "checks": "failed"})
        )
        self.assertEqual(self.fix_on_pull()["verified_sha"], "b" * 40)

    def test_go_refuses_when_pull_request_is_open(self):
        self.gh.add_pull(9, 5)
        decision = route(self.gh, "issue_comment", comment_event(5, "/nexkit go"))
        self.assertEqual(decision["action"], "none")
        self.assertIn("#9", decision["reason"])
        self.assertEqual(decision["reply_to"], 5)

    def test_requires_write_access(self):
        decision = route(self.gh, "issue_comment", comment_event(5, "/nexkit go", login="bob"))
        self.assertEqual(decision["action"], "none")
        self.assertIn("write access", decision["reason"])
        self.assertEqual(decision["reply_to"], 5)

    def test_ignores_ordinary_and_bot_comments(self):
        self.assertIsNone(route(self.gh, "issue_comment", comment_event(5, "thanks"))["reply_to"])
        bot = comment_event(5, "/nexkit go", login=BOT_LOGIN, user_type="Bot")
        self.assertEqual(route(self.gh, "issue_comment", bot)["action"], "none")
        other_bot = comment_event(5, "/nexkit go", login="ci[bot]", user_type="Bot")
        self.assertEqual(route(self.gh, "issue_comment", other_bot)["action"], "none")

    def test_closed_issue_and_unknown_command(self):
        self.gh.add_issue(6, state="closed")
        self.assertIn(
            "closed", route(self.gh, "issue_comment", comment_event(6, "/nexkit go"))["reason"]
        )
        unknown = route(self.gh, "issue_comment", comment_event(5, "/nexkit deploy"))
        self.assertEqual(unknown["action"], "none")
        self.assertIn("not an issue command", unknown["reason"])

    def test_fix_and_review_on_nexkit_pull_request(self):
        self.gh.add_pull(9, 5, head_sha="b" * 40)
        fix = route(
            self.gh, "issue_comment", comment_event(9, "/nexkit fix rename it", on_pull=True)
        )
        self.assertEqual(fix["action"], "fix")
        self.assertEqual((fix["issue"], fix["pr"], fix["target"]), (5, 9, 9))
        self.assertEqual(fix["head"], "b" * 40)
        self.assertEqual(fix["branch"], "nexkit/issue-5")
        self.assertEqual(fix["note"], "rename it")
        review = route(self.gh, "issue_comment", comment_event(9, "/nexkit review", on_pull=True))
        self.assertEqual(review["action"], "review")

    def test_rejects_foreign_and_closed_pull_requests(self):
        self.gh.add_pull(10, 5, branch="feature/x")
        self.gh.add_pull(11, 5, repo="someone/fork")
        self.gh.add_pull(12, 5, state="closed")
        for number in (10, 11):
            decision = route(
                self.gh, "issue_comment", comment_event(number, "/nexkit fix", on_pull=True)
            )
            self.assertIn("not opened by NexKit", decision["reason"])
        closed = route(self.gh, "issue_comment", comment_event(12, "/nexkit fix", on_pull=True))
        self.assertIn("closed", closed["reason"])
        plan = route(self.gh, "issue_comment", comment_event(10, "/nexkit plan", on_pull=True))
        self.assertIn("not a pull request command", plan["reason"])

    def test_changes_requested_review_starts_fix(self):
        self.gh.add_pull(9, 5)
        event = {
            "action": "submitted",
            "review": {
                "state": "changes_requested",
                "body": "Handle None",
                "user": {"login": "olivia"},
            },
            "pull_request": {"number": 9},
        }
        decision = route(self.gh, "pull_request_review", event)
        self.assertEqual(decision["action"], "fix")
        self.assertEqual(decision["note"], "Handle None")
        event["review"]["state"] = "approved"
        self.assertEqual(route(self.gh, "pull_request_review", event)["action"], "none")
        event["review"].update(state="changes_requested", user={"login": "bob"})
        self.assertEqual(route(self.gh, "pull_request_review", event)["action"], "none")

    def test_dispatch(self):
        self.gh.add_pull(9, 5)
        event = {"sender": {"login": BOT_LOGIN}}
        decision = route(
            self.gh, "workflow_dispatch", event, {"command": "fix", "number": "9", "auto": "true"}
        )
        self.assertEqual(decision["action"], "fix")
        self.assertTrue(decision["auto"])
        self.gh.add_issue(6)
        manual = route(self.gh, "workflow_dispatch", event, {"command": "go", "number": "6"})
        self.assertEqual(manual["action"], "implement")
        self.assertFalse(manual["auto"])
        bad = route(self.gh, "workflow_dispatch", event, {"command": "go", "number": "x"})
        self.assertEqual(bad["action"], "none")
        wait = route(self.gh, "workflow_dispatch", event, {"command": "resume", "number": "resume"})
        self.assertEqual(wait, {"action": "maintain", "task": "resume"})

    def test_a_pull_request_that_changes_nothing_goes_to_maintain(self):
        self.gh.add_pull(9, 5, head_sha="c" * 40)
        self.gh.on_base.add("c" * 40)
        event = {"sender": {"login": BOT_LOGIN}}
        inputs = {"command": "review", "number": "9", "auto": "true"}
        self.assertEqual(
            route(self.gh, "workflow_dispatch", event, inputs),
            {
                "action": "maintain",
                "task": "on_base",
                "issue": 5,
                "pr": 9,
                "base": "main",
                "head": "c" * 40,
                "merge_base": "c" * 40,
                "actor": BOT_LOGIN,
            },
        )
        fix = route(self.gh, "issue_comment", comment_event(9, "/nexkit fix", on_pull=True))
        self.assertEqual((fix["action"], fix["task"]), ("maintain", "on_base"))
        # A change of its own, however small, or a comparison GitHub cannot make, is
        # handled as usual.
        self.gh.on_base.clear()
        self.assertEqual(route(self.gh, "workflow_dispatch", event, inputs)["action"], "review")
        self.gh.compare = mock.Mock(side_effect=GitHubError(500, "Server Error"))
        self.assertEqual(route(self.gh, "workflow_dispatch", event, inputs)["action"], "review")

    def test_dispatch_marks_resumed_and_conflict_rounds(self):
        self.gh.add_pull(9, 5)
        event = {"sender": {"login": BOT_LOGIN}}
        inputs = {"command": "fix", "number": "9", "auto": "true,resume"}
        decision = route(self.gh, "workflow_dispatch", event, inputs)
        self.assertEqual((decision["auto"], decision["resumed"]), (True, True))
        self.assertNotIn("conflicts", decision)
        self.assertEqual(auto_input(decision, resume=True), "true,resume")
        inputs["auto"] = "resume"
        decision = route(self.gh, "workflow_dispatch", event, inputs)
        self.assertEqual((decision["auto"], decision["resumed"]), (False, True))
        self.assertEqual(auto_input(decision), "false")
        inputs["auto"] = "conflicts"
        decision = route(self.gh, "workflow_dispatch", event, inputs)
        self.assertEqual((decision["auto"], decision["conflicts"]), (True, True))
        self.assertEqual(auto_input(decision, resume=True), "conflicts,resume")
        inputs["auto"] = "true,retry"
        decision = route(self.gh, "workflow_dispatch", event, inputs)
        self.assertEqual((decision["auto"], decision["retry"]), (True, True))
        # A retry that pauses at the usage limit resumes as the retry it was.
        self.assertEqual(auto_input(decision, resume=True), "true,resume,retry")

    def test_the_review_after_a_branch_update_may_carry_the_approval_over(self):
        self.gh.add_pull(9, 5, head_sha="e" * 40)
        event = {"sender": {"login": BOT_LOGIN}}
        inputs = {"command": "review", "number": "9", "note": "", "auto": "true,carry"}
        # Without an approving review there is nothing to carry over.
        self.assertNotIn("carry_from", route(self.gh, "workflow_dispatch", event, inputs))
        state = empty_state(5)
        state["last_review"] = {"round": 1, "head": "a" * 40, "verdict": "approve", "findings": []}
        self.gh.comment(9, render_state(state))
        decision = route(self.gh, "workflow_dispatch", event, inputs)
        self.assertEqual(decision["carry_from"], "a" * 40)
        self.assertTrue(decision["auto"])
        # The one more try after a transient error may carry it over too.
        self.assertEqual(auto_input(decision, retry=True), "true,retry,carry")
        # Only that review: not one the branch update did not start, nor a fix round.
        inputs["auto"] = "true"
        self.assertNotIn("carry_from", route(self.gh, "workflow_dispatch", event, inputs))
        inputs.update(auto="true,carry", command="fix")
        self.assertNotIn("carry_from", route(self.gh, "workflow_dispatch", event, inputs))
        # Not a review that requested changes, nor one of the head itself.
        for verdict, head in (("request_changes", "a" * 40), ("approve", "e" * 40)):
            state["last_review"].update(verdict=verdict, head=head)
            self.gh.comment(9, render_state(state))
            inputs["command"] = "review"
            self.assertNotIn("carry_from", route(self.gh, "workflow_dispatch", event, inputs))

    def test_schedule_is_maintenance(self):
        self.assertEqual(route(self.gh, "schedule", {}), {"action": "maintain", "task": "schedule"})

    def test_other_events_are_ignored(self):
        self.assertEqual(route(self.gh, "push", {})["action"], "none")


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        self.gh.add_issue(5)
        self.cfg = profile_config()

    def plan(self, profile=None, chosen_by="triage"):
        record = profile_record({"profile": profile, "chosen_by": chosen_by}) if profile else ""
        body = f"{PLAN_MARKER}\n{record}\n## Plan\n\nAdd it.\n\n{profile_text('Profile.')}"
        self.gh.comment(5, body)

    def test_a_profile_record_in_agent_text_does_not_count(self):
        fake = profile_record({"profile": "hard", "chosen_by": "triage"})
        self.gh.comment(5, f"{PLAN_MARKER}\n## Plan\n\n{fake}\nAdd it.")
        self.assertEqual(self.resolve("go")[0]["profile"], "standard")

    def resolve(self, command, number=5):
        if number == 9 and 9 not in self.gh.pulls:
            self.gh.add_pull(9, 5)
        event = comment_event(number, f"/nexkit {command}", on_pull=number == 9)
        return with_profile(self.gh, route(self.gh, "issue_comment", event), self.cfg)

    def test_rounds_use_the_latest_plans_profile(self):
        self.plan("standard")
        self.plan("hard")
        for command, number in (("go", 5), ("fix", 9), ("review", 9)):
            decision, cfg = self.resolve(command, number)
            self.assertEqual(decision["profile"], "hard", command)
            self.assertEqual(config.stage(cfg, "review")["model"], "fable", command)
            self.assertEqual(config.stage(cfg, "fix")["max_budget_usd"], 20, command)

    def test_rounds_without_a_profile_use_the_default(self):
        decision, cfg = self.resolve("go")
        self.assertEqual(decision["profile"], "standard")
        self.assertEqual(config.stage(cfg, "implement")["model"], "sonnet")
        self.plan()  # a plan from before profiles
        self.assertEqual(self.resolve("fix", 9)[0]["profile"], "standard")

    def test_rounds_stop_when_the_profile_is_gone(self):
        self.plan("expert")
        decision, _ = self.resolve("fix", 9)
        self.assertEqual(decision["action"], "none")
        self.assertEqual(decision["reply_to"], 9)
        self.assertIn("`expert`, which is no longer", decision["reason"])
        self.assertIn("`/nexkit plan` on #5", decision["reason"])

    def test_plan_gets_the_profile_to_keep(self):
        decision, cfg = self.resolve("plan")
        self.assertIsNone(decision["previous_profile"])
        self.assertNotIn("profile", decision)
        self.assertEqual(cfg, self.cfg)
        self.plan("hard", chosen_by="previous")
        self.assertEqual(self.resolve("plan")[0]["previous_profile"], "hard")
        # A fallback after a failed triage, or a removed profile, is not kept.
        self.plan("standard", chosen_by="default")
        self.assertIsNone(self.resolve("plan")[0]["previous_profile"])
        self.plan("expert")
        self.assertIsNone(self.resolve("plan")[0]["previous_profile"])

    def test_without_profiles_nothing_changes(self):
        self.plan("hard")
        decision = route(self.gh, "issue_comment", comment_event(5, "/nexkit go"))
        cfg = make_config()
        self.assertEqual(with_profile(self.gh, decision, cfg), (decision, cfg))


class SkipUnaffectedTests(unittest.TestCase):
    """`route` leaves out a check of an automatic review whose paths did not change since
    it passed, and runs it at any doubt."""

    PASSED, HEAD = "a" * 40, "e" * 40

    def setUp(self):
        self.gh = FakeGitHub()
        self.gh.add_issue(5)
        self.gh.add_pull(6, 5, head_sha=self.HEAD)
        self.cfg = make_config(
            setup=["npm ci"],
            checks=[
                {"name": "unit", "run": "make unit"},
                {"name": "e2e", "run": "make e2e", "unaffected_by": ["docs/**", "**/*.md"]},
            ],
        )
        self.setup = setup_fingerprint(["npm ci"])
        passed = {"unit": check_fingerprint("make unit"), "e2e": check_fingerprint("make e2e")}
        self.gh.round_comment(6, 1, self.PASSED, "passed", passed=passed, setup=self.setup)
        self.changed(["docs/guide.md", "README.md"])

    def changed(self, paths, status="ahead", **fields):
        files = [{"filename": path, **fields} for path in paths]
        self.gh.compares[(self.PASSED, self.HEAD)] = {"status": status, "files": files}

    def decide(self, auto="true,carry", cfg=None):
        inputs = {"command": "review", "number": "6", "note": "", "auto": auto}
        decision = route(self.gh, "workflow_dispatch", {"sender": {"login": BOT_LOGIN}}, inputs)
        return skip_unaffected(self.gh, decision, cfg or self.cfg)

    def test_a_check_whose_paths_did_not_change_since_it_passed_is_skipped(self):
        decision = self.decide()
        self.assertEqual(decision["skipped_checks"], [{"name": "e2e", "since": self.PASSED}])
        self.assertEqual(decision["setup"], self.setup)
        # Any automatic review: also after a new commit while a merge waits, or a retry.
        self.assertIn("skipped_checks", self.decide(auto="true,retry"))
        # The same head: nothing changed since it passed there.
        self.gh.compares[(self.PASSED, self.HEAD)] = {"status": "identical", "files": []}
        self.assertIn("skipped_checks", self.decide())

    def test_any_doubt_runs_the_check(self):
        cases = [
            ("a path it reads", lambda: self.changed(["docs/a.md", "src/app.py"])),
            (
                "a rename from a path it reads",
                lambda: self.changed(["docs/app.md"], previous_filename="src/app.py"),
            ),
            (
                "a head that does not descend from it",
                lambda: self.changed(["docs/a.md"], status="diverged"),
            ),
            (
                "a comparison that may leave files out",
                lambda: self.changed([f"docs/{i}.md" for i in range(COMPARE_FILES)]),
            ),
            (
                "a comparison without files",
                lambda: self.gh.compares.update({(self.PASSED, self.HEAD): {"status": "ahead"}}),
            ),
            (
                "a comparison that fails",
                lambda: self.gh.compares.update(
                    {(self.PASSED, self.HEAD): GitHubError(404, "No commit found")}
                ),
            ),
        ]
        compare = self.gh.compare

        def failing(base, head):
            found = compare(base, head)
            if isinstance(found, Exception):
                raise found
            return found

        self.gh.compare = failing
        for name, change in cases:
            with self.subTest(name):
                self.changed(["docs/guide.md"])
                change()
                self.assertNotIn("skipped_checks", self.decide())

    def test_a_person_or_another_round_runs_every_check(self):
        self.assertNotIn("skipped_checks", self.decide(auto="false"))
        comment = comment_event(6, "/nexkit review", on_pull=True)
        decision = skip_unaffected(self.gh, route(self.gh, "issue_comment", comment), self.cfg)
        self.assertNotIn("skipped_checks", decision)
        inputs = {"command": "fix", "number": "6", "note": "", "auto": "true"}
        fix = route(self.gh, "workflow_dispatch", {"sender": {"login": BOT_LOGIN}}, inputs)
        decision = skip_unaffected(self.gh, fix, self.cfg)
        self.assertNotIn("skipped_checks", decision)
        self.assertEqual(decision["setup"], self.setup)

    def test_only_the_checks_own_record_counts(self):
        # Its command changed since it passed: no record of this command.
        cfg = make_config(
            setup=["npm ci"],
            checks=[{"name": "e2e", "run": "make e2e -x", "unaffected_by": ["docs/**"]}],
        )
        self.assertNotIn("skipped_checks", self.decide(cfg=cfg))
        # Another setup ran then.
        cfg = make_config(
            setup=["pnpm i"], checks=[{"name": "e2e", "run": "make e2e", "unaffected_by": ["**"]}]
        )
        self.assertNotIn("skipped_checks", self.decide(cfg=cfg))
        # A check added since, which never passed: rounds whose checks all passed do not
        # count for it.
        cfg = make_config(
            setup=["npm ci"], checks=[{"name": "new", "run": "make new", "unaffected_by": ["**"]}]
        )
        self.assertNotIn("skipped_checks", self.decide(cfg=cfg))

    def test_a_round_from_before_setup_was_recorded_gives_no_record(self):
        gh = self.gh
        gh.issue_comments[6] = []
        passed = {"e2e": check_fingerprint("make e2e")}
        gh.round_comment(6, 1, self.PASSED, "passed", passed=passed)
        self.assertNotIn("skipped_checks", self.decide())
        # The round after it records its setup, so the next one may skip.
        gh.round_comment(6, 2, self.PASSED, "passed", passed=passed, setup=self.setup)
        self.assertIn("skipped_checks", self.decide())

    def test_without_unaffected_by_nothing_is_compared(self):
        cfg = make_config(checks=[{"name": "unit", "run": "make unit"}])
        inputs = {"command": "review", "number": "6", "note": "", "auto": "true"}
        decision = route(self.gh, "workflow_dispatch", {"sender": {"login": BOT_LOGIN}}, inputs)
        self.gh.compare = mock.Mock(side_effect=AssertionError("compared"))
        decision = skip_unaffected(self.gh, decision, cfg)
        self.assertNotIn("skipped_checks", decision)
        self.assertEqual(decision["setup"], setup_fingerprint([]))


if __name__ == "__main__":
    unittest.main()
