import unittest

from nexkit import config
from nexkit.route import parse_command, route, with_profile
from nexkit.state import BOT_LOGIN, PLAN_MARKER, profile_record, profile_text
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


if __name__ == "__main__":
    unittest.main()
