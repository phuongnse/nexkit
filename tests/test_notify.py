"""Who is mentioned when a run needs a person (`notify`)."""

import unittest

from nexkit.autopilot import mention_text, person
from nexkit.config import ConfigError, validate
from nexkit.report import NEEDS_PERSON, READY, needs_person, report
from tests.support import CLAUDE, make_config
from tests.test_report import HEAD, LIMIT, ReportCase, decision, review_result

WITH_CLAUDE = {"agents": CLAUDE}


class RuleTests(unittest.TestCase):
    """One case per row of the table in docs/how-it-works.md. Each reason says what a
    person does, then why NexKit does not (`autopilot.person`)."""

    def setUp(self):
        self.cfg = make_config()

    def assertReason(self, reason, do, why):
        self.assertTrue(reason.startswith(do), reason)
        self.assertIn(why, reason)

    def test_planned_with_questions_or_too_large(self):
        self.assertEqual(
            needs_person("planned", self.cfg, plan={"questions": ["Which?"]}),
            "answer the plan's questions with `/nexkit plan <answers>`. The plan asks for "
            "decisions that are the owner's to make: `Which?`",
        )
        self.assertReason(
            needs_person("planned", self.cfg, plan={"too_large": True}),
            "split the issue by hand",
            "The plan says the issue is too large for one session",
        )

    def test_planned_without_either(self):
        self.assertIsNone(needs_person("planned", self.cfg, plan={"questions": []}))

    def test_agent_blocked_names_the_decision_and_the_command(self):
        found = {"questions": [{"question": "Keep @alice's API?"}, {"question": "Rename it?"}]}
        self.assertEqual(
            needs_person("agent_blocked", self.cfg, found=found),
            "answer the questions in the run's comment with `/nexkit plan <answers>`, or "
            "`/nexkit go <answers>` when they leave the plan as it is. The agent stopped at a "
            "decision that is the owner's to make: `Keep @alice's API?` and 1 more",
        )
        self.assertTrue(
            needs_person("agent_blocked", self.cfg, found={"pull": True}).startswith(
                "answer the questions in the round's comment with `/nexkit fix <answers>`."
            )
        )

    def test_every_outcome_that_needs_a_person_says_what_to_do_and_why(self):
        for outcome, (do, why) in NEEDS_PERSON.items():
            with self.subTest(outcome):
                reason = needs_person(outcome, self.cfg)
                self.assertEqual(reason, person(do, why))
                text = mention_text(make_config(notify=["owner"]), reason)
                self.assertTrue(text.startswith(f"@owner a person is needed: {do}. "), text)
                self.assertTrue(text.endswith(f"{why[1:]}."), text)

    def test_errors_are_named_in_code_spans(self):
        for outcome in ("agent_error", "publish_failed", "close_failed", "retry_failed"):
            reason = needs_person(outcome, self.cfg, found={"error": "boom\n@bob <!-- x -->"})
            self.assertTrue(reason.endswith(": `boom @bob ‹!-- x --›`"), reason)

    def test_an_agent_that_ran_out_of_time(self):
        self.assertReason(
            needs_person("agent_error", self.cfg, found={"timed_out": True}),
            "comment `/nexkit plan` to split the issue",
            "The agent ran out of time before it finished",
        )

    def test_a_review_that_cannot_sign_in_says_another_try_stops_the_same_way(self):
        self.assertReason(
            needs_person("review_failed", self.cfg, found={"error": "401 Unauthorized"}),
            "fix the review agent's secret, then comment `/nexkit review`",
            "another try stops the same way: `401 Unauthorized`",
        )
        self.assertReason(
            needs_person("review_failed", self.cfg, found={"error": "no verdict"}),
            "comment `/nexkit review` to run the review again",
            "after an error of the service, the network or the runner: `no verdict`",
        )

    def test_needs_human_names_what_still_fails(self):
        finding = {"severity": "blocking", "file": "app.py", "line": 3, "body": "x"}
        found = {"failing": ["unit", "lint"], "findings": [finding, finding]}
        self.assertEqual(
            needs_person("needs_human", self.cfg, found=found),
            "comment `/nexkit fix <instructions>` for what still fails, push a commit "
            "yourself, or close the pull request. Checks `unit`, `lint` fail and the review "
            "has 2 blocking findings, the first at `app.py:3`, and no automatic fix rounds "
            "remain",
        )

    def test_ready_only_without_auto_merge(self):
        self.assertReason(
            needs_person("ready", self.cfg),
            "review the pull request, then merge it or close it",
            "with `merge.auto` off a person decides the merge",
        )
        self.assertIsNone(needs_person("ready", make_config(merge={"auto": True})))

    def test_ready_with_criteria_to_confirm(self):
        for cfg, then in (
            (self.cfg, "then merge the pull request."),
            (make_config(merge={"auto": True}), "then approve the pull request, which NexKit"),
        ):
            reason = needs_person("ready", cfg, confirm=["passes five runs", "is fast"])
            self.assertReason(
                reason,
                "confirm from the diff and the checks the acceptance criteria",
                "The review could not verify `passes five runs` and 1 more",
            )
            self.assertIn(then, reason)

    def test_merge_refused_says_what_unblocks_it(self):
        found = {"refused": ("approve it", "GitHub wants a review")}
        self.assertEqual(
            needs_person("merge_refused", self.cfg, found=found),
            "approve it. GitHub wants a review",
        )

    def test_merged_and_automatic_rounds(self):
        for outcome in ("merged", "auto_fix", "conflict_fix", "up_to_date"):
            self.assertIsNone(needs_person(outcome, self.cfg), outcome)

    def test_paused_and_resuming(self):
        self.assertIsNone(needs_person("paused", self.cfg, resume={"at": "x"}))

    def test_paused_without_resume(self):
        self.assertIn("usage limit", needs_person("paused", self.cfg))


class ConfigTests(unittest.TestCase):
    def test_notify(self):
        self.assertEqual(
            validate(
                {
                    **WITH_CLAUDE,
                }
            )["notify"],
            [],
        )
        self.assertEqual(validate({**WITH_CLAUDE, "notify": ["octo-cat"]})["notify"], ["octo-cat"])
        for bad in (["@owner"], "owner", ["a", "A"], [""], ["-x"]):
            with self.assertRaises(ConfigError, msg=bad):
                validate({**WITH_CLAUDE, "notify": bad})


class MentionTests(ReportCase):
    def mentions(self, number):
        return [c["body"] for c in self.gh.comments(number) if c["body"].startswith("@")]

    def test_a_ready_pull_request_mentions_the_listed_people_once(self):
        self.candidate()
        self.run_report(cfg=make_config(notify=["owner", "lead"]))
        self.assertEqual(self.mentions(6), [f"@owner @lead a person is needed: {READY}."])
        self.assertEqual(self.mentions(5), [])
        [row] = self.gh.run_comments(6)
        self.assertEqual(row["run"]["attention"], READY)

    def test_nobody_is_mentioned_without_notify(self):
        self.candidate()
        self.run_report()
        self.assertEqual(self.mentions(6), [])
        # The reason is still recorded for `nexkit status`.
        self.assertIn("attention", self.gh.run_comments(6)[0]["run"])

    def test_a_plan_with_questions_mentions_on_the_issue(self):
        self.write(
            "nexkit-agent", "result.json", {"status": "done", "output": {"questions": ["?"]}}
        )
        d = decision("plan")
        needs = {"publish": {"outputs": {"result": '{"published": true, "comment": "u"}'}}}
        report(
            self.gh,
            d,
            make_config(notify=["owner"]),
            needs,
            self.artifacts,
            workflow_ref="x/nexkit.yml@main",
            default_branch="main",
        )
        reason = (
            "answer the plan's questions with `/nexkit plan <answers>`. The plan asks for "
            "decisions that are the owner's to make: `?`"
        )
        self.assertEqual(self.mentions(5), [f"@owner a person is needed: {reason}."])
        [run] = self.gh.run_comments(5)
        self.assertEqual(run["run"]["attention"], reason)

    def test_automatic_rounds_and_resumed_pauses_mention_nobody(self):
        finding = {"severity": "blocking", "file": "a", "line": 1, "body": "x"}
        self.candidate(checks_pass=False, review=review_result("request_changes", [finding]))
        self.run_report(cfg=make_config(notify=["owner"]))
        self.write("nexkit-agent", "result.json", LIMIT)
        d = decision("fix", pr=6, target=6, head=HEAD)
        self.run_report(d=d, cfg=make_config(notify=["owner"]), published=False)
        self.assertEqual(self.mentions(6), [])

    def test_a_blocked_fix_round_mentions_on_the_pull_request(self):
        self.write("nexkit-agent", "result.json", {"status": "blocked", "error": "Which way?"})
        d = decision("fix", pr=6, target=6, head=HEAD)
        self.run_report(d=d, cfg=make_config(notify=["owner"]), published=False)
        self.assertEqual(
            self.mentions(6),
            [
                "@owner a person is needed: answer the questions in the round's comment with "
                "`/nexkit fix <answers>`. The agent stopped at a decision that is the owner's "
                "to make."
            ],
        )


if __name__ == "__main__":
    unittest.main()
